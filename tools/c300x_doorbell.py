#!/usr/bin/env python3
"""
c300x_doorbell.py — rileva lo squillo del videocitofono BTicino/Legrand Classe 300X
e cattura uno screenshot dalla telecamera. SENZA root, SENZA firmware modificato.

Ricostruito via reverse engineering dell'app ufficiale "Door Entry" 1.17.5
(package com.legrandgroup.c300x). Replica ciò che fa l'app:

COME FUNZIONA (dal bytecode):
  * Quando qualcuno suona, il citofono fa partire una CHIAMATA SIP verso il tuo account.
    Arriva anche una push Firebase, ma serve solo a svegliare l'app e farla ri-registrare:
    l'evento vero è l'INVITE SIP in ingresso da  sip:c300x@<GatewayId>.bs.iotleg.com.
    => "suonano alla porta" == "ricevo una chiamata SIP". Nessun polling necessario.
  * Il video è dentro la chiamata. L'app accetta con video in sola ricezione
    (setVideoEnabled(true), setVideoDirection(RecvOnly)) e aggiunge gli attributi SDP
    custom  DEVADDR=<posto esterno>  e  CAMERASLIDING=1.  Da quel flusso H.264 si cattura
    un fotogramma (equivalente di Call.takeVideoSnapshot dell'app).

Due parti, come prima:
  STEP 1  — login REST su myhomeweb.com e recupero credenziali SIP. Vedi c300x_client.py
            (riusa quello per ottenere SipAccount/SipPassword/GatewayId). Testabile da solo.
  STEP 2  — registrazione SIP su TLS + attesa INVITE + accept video-only + snapshot.
            Richiede un motore SIP. Qui sotto trovi DUE implementazioni possibili:
              (A) linphone (python-linphone / linphonec)  <- consigliata, è ciò che usa l'app
              (B) baresip + modulo per lo snapshot
            e un abbozzo di orchestrazione che puoi adattare.

PERCHÉ non basta urllib come nello step 1:
  SIP+TLS+(S)RTP e il decode H.264 non si fanno con la libreria standard. Serve un client SIP
  che sappia negoziare il video e scrivere un frame su file. linphone lo fa già.

----------------------------------------------------------------------------------------------
ORCHESTRAZIONE (pseudo-reale con linphone). Da completare con le tue credenziali dallo step 1.
----------------------------------------------------------------------------------------------
"""

import sys
import time

# Costanti di protocollo (identiche all'app) ---------------------------------
SIP_DOMAIN_SUFFIX = ".bs.iotleg.com"   # dominio/realm SIP = <GatewayId> + questo
SIP_TLS_PORT = 5061
PEER_LOCALPART = "c300x"               # le chiamate del citofono arrivano da sip:c300x@<dominio>


def build_sip_identity(sip_account: str) -> str:
    """SipAccount dallo step 1 ha già forma  <utente>-<mac>@<gwid>.bs.iotleg.com .
    L'identity SIP è semplicemente  sip:<SipAccount>."""
    return f"sip:{sip_account}"


def build_auth(sip_account: str, sip_password: str, gateway_id: str):
    """Ritorna (username, password, realm) per l'autenticazione SIP.
    username = parte locale del SipAccount (prima della @); realm = dominio del gateway."""
    username = sip_account.split("@", 1)[0]
    realm = f"{gateway_id}{SIP_DOMAIN_SUFFIX}"
    return username, sip_password, realm


# ---------------------------------------------------------------------------
# Implementazione A — linphone (consigliata)
# ---------------------------------------------------------------------------
# Requisiti (Debian/Ubuntu/HAOS container):   apt install linphone  (fornisce linphonec)
# oppure il binding python:   pip install linphone   (dove disponibile)
#
# Logica:
#   1) crea un Core linphone, imposta transport TLS, carica il certificato client
#      ottenuto da POST /eliot/users/cert/signcert (file .cert.pem + chiave + ca-chain).
#   2) registra l'account (identity/auth come sopra).
#   3) resta in ascolto: alla CallState "IncomingReceived" da PEER_LOCALPART:
#        - logga/notifica "SUONANO ALLA PORTA" (+ timestamp)
#        - accetta con video RecvOnly, audio Inactive
#        - dopo ~1.5s (keyframe arrivato) chiama takeVideoSnapshot(path)
#        - termina la chiamata
#   4) emette lo screenshot su file / webhook.
#
# NB: l'app abilita SRTP (prefs_media_encryption_mandatory=true). In linphone:
#     core.media_encryption = MediaEncryption.SRTP  (o DTLS, da verificare col tuo impianto).

def run_with_linphone(cfg, on_ring, snapshot_path="/tmp/doorbell.jpg"):
    """cfg: dict con sip_account, sip_password, gateway_id,
            cert_pem_path, key_pem_path, ca_chain_path.
       on_ring: callback chiamata quando squillano (riceve il nome del chiamante).
    Questo è uno scheletro: i nomi esatti dei metodi dipendono dalla versione del binding.
    """
    try:
        import linphone  # type: ignore
    except ImportError:
        print("Manca il modulo 'linphone'. Installa linphone o usa linphonec (vedi commenti).",
              file=sys.stderr)
        return 1

    identity = build_sip_identity(cfg["sip_account"])
    username, password, realm = build_auth(
        cfg["sip_account"], cfg["sip_password"], cfg["gateway_id"]
    )
    domain = f'{cfg["gateway_id"]}{SIP_DOMAIN_SUFFIX}'
    proxy = f"sip:{domain};transport=tls"

    snapshot_taken = {"done": False}

    def global_cb(core, state, message):
        pass

    def call_state_cb(core, call, state, message):
        # Stati rilevanti: IncomingReceived, StreamsRunning, End/Released
        st = str(state)
        remote = call.remote_address_as_string if hasattr(call, "remote_address_as_string") else ""
        if "IncomingReceived" in st:
            if PEER_LOCALPART in (remote or ""):
                on_ring(remote)
                params = core.create_call_params(call)
                params.video_enabled = True
                try:
                    params.video_direction = linphone.MediaDirection.RecvOnly
                    params.audio_direction = linphone.MediaDirection.Inactive
                except Exception:
                    pass
                core.accept_call_with_params(call, params)
        elif "StreamsRunning" in st and not snapshot_taken["done"]:
            # il video scorre: aspetta un attimo il keyframe, poi cattura
            time.sleep(1.5)
            try:
                call.take_video_snapshot(snapshot_path)
                snapshot_taken["done"] = True
                print(f"Screenshot salvato in {snapshot_path}")
            except Exception as e:
                print(f"take_video_snapshot fallito: {e}", file=sys.stderr)
            # chiudi la chiamata: ci interessava solo il frame
            core.terminate_call(call)

    callbacks = {
        "global_state_changed": global_cb,
        "call_state_changed": call_state_cb,
    }
    core = linphone.Core.new(callbacks, None, None)
    # transport: solo TLS
    tr = core.sip_transports
    tr.udp_port = 0
    tr.tcp_port = 0
    tr.tls_port = SIP_TLS_PORT
    core.sip_transports = tr

    # certificato client per il mutual-TLS verso il server Legrand
    core.root_ca = cfg["ca_chain_path"]
    try:
        core.tls_cert_path = cfg["cert_pem_path"]
        core.tls_key_path = cfg["key_pem_path"]
    except Exception:
        pass
    try:
        core.media_encryption = linphone.MediaEncryption.SRTP
    except Exception:
        pass

    # crea e registra l'account
    cfg_proxy = core.create_proxy_config()
    cfg_proxy.identity_address = core.create_address(identity)
    cfg_proxy.server_addr = proxy
    cfg_proxy.route = proxy
    cfg_proxy.register_enabled = True
    auth = core.create_auth_info(username, None, password, None, realm, domain)
    core.add_auth_info(auth)
    core.add_proxy_config(cfg_proxy)
    core.default_proxy_config = cfg_proxy

    print(f"In ascolto come {identity} su {proxy} … (Ctrl-C per uscire)")
    try:
        while True:
            core.iterate()
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nChiudo.")
    return 0


# ---------------------------------------------------------------------------
# Implementazione B — linphonec (riga di comando), se non hai il binding python
# ---------------------------------------------------------------------------
# linphonec legge un file di config ~/.linphonerc. Esempio di sezioni da generare:
#
#   [sip]
#   contact=sip:<SipAccount>
#   sip_tcp_port=0
#   sip_udp_port=0
#   sip_tls_port=5061
#   media_encryption=srtp
#   root_ca=/percorso/ca-chain.cert.pem
#   client_cert_chain=/percorso/<SipAccount>.cert.pem
#   client_cert_key=/percorso/<SipAccount>.key.pem
#
#   [auth_info_0]
#   username=<parte locale di SipAccount>
#   passwd=<SipPassword>
#   realm=<GatewayId>.bs.iotleg.com
#
#   [proxy_0]
#   reg_proxy=<sip:GatewayId.bs.iotleg.com;transport=tls>
#   reg_identity=sip:<SipAccount>
#   reg_sendregister=1
#
# Poi, in interattivo o via pipe, abilitare il video, su "Incoming call" dare:
#   answer
#   (dopo qualche secondo)  snapshot /tmp/doorbell.jpg
#   terminate
# Il comando `snapshot` di linphonec salva un JPEG del frame corrente.
# È la via più rapida per un primo test manuale dell'intera catena.

def main():
    print(__doc__)
    print("\nQuesto file è uno scheletro operativo, non un eseguibile 'chiavi in mano'.")
    print("Passi consigliati:")
    print("  1) Lancia prima:  python3 c300x_client.py discover <email> <password>")
    print("     e annota SipAccount, SipPassword, GatewayId.")
    print("  2) Ottieni il certificato client (POST /eliot/users/cert/signcert).")
    print("  3) Testa la catena con linphonec (Implementazione B) prima di automatizzare.")
    print("\nSe vuoi, dammi l'output dello step 1 (oscura password e MAC) e ti preparo")
    print("il ~/.linphonerc già compilato e lo script che risponde + fa snapshot in automatico.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
