#!/usr/bin/env python3
r"""
c300x_sip_register.py — test di REGISTRAZIONE SIP al videocitofono Classe 300X, in Python puro.
Gira su Windows/Mac/Linux senza installare linphone: usa solo socket + ssl + hashlib standard.

A COSA SERVE:
  Verifica il pezzo ancora non testato della catena: che il certificato client, il TLS mutuo
  e l'autenticazione digest funzionino davvero contro il server SIP di Legrand.
  NON riceve la chiamata e NON cattura video (per quello serve un vero motore SIP: vedi note in
  fondo). È il "ping" che conferma che le credenziali e il certificato sono validi.

PARAMETRI (dallo step precedente, cartella c300x-certs):
  --account   il tuo SipAccount completo (es. nome-...-XXXX@2030718.bs.iotleg.com)
  --password  la SipPassword
  --cert      <SipAccount>.cert.pem
  --key       <SipAccount>.key.pem
  --cafile    ca-chain.cert.pem  (opzionale; se assente o inaffidabile usa --insecure)

DETTAGLI DI PROTOCOLLO (dall'app, VctLinphoneService.registerToDefaultAccount):
  * Server SIP (registrar):  app.sipserver.bs.iotleg.com : 5061  (TLS)
  * Identity / AOR (From/To):  sip:<SipAccount>
  * Digest: username = parte locale del SipAccount (prima della @), password = SipPassword,
            realm = quello che restituisce il server nel 401 (l'app non lo forza).
  * TLS mutuo: il client presenta <SipAccount>.cert.pem + chiave; il server è verificato
    contro ca-chain.cert.pem (subject atteso: *.bs.iotleg.com).

USO TIPICO:
  cd c300x-certs
  python ..\c300x_sip_register.py ^
     --account "finetti...@2030718.bs.iotleg.com" ^
     --password "LA_SIP_PASSWORD" ^
     --cert "finetti...@2030718.bs.iotleg.com.cert.pem" ^
     --key  "finetti...@2030718.bs.iotleg.com.key.pem" ^
     --cafile "ca-chain.cert.pem" -v

Se compare "REGISTRAZIONE RIUSCITA (200 OK)" l'intera catena cloud→cert→SIP funziona.
"""

import argparse
import hashlib
import os
import random
import re
import socket
import ssl
import sys
import uuid

SIP_SERVER = "app.sipserver.bs.iotleg.com"
SIP_PORT = 5061


def H(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def digest_response(username, realm, password, method, uri, nonce,
                    qop=None, cnonce=None, nc=None):
    ha1 = H(f"{username}:{realm}:{password}")
    ha2 = H(f"{method}:{uri}")
    if qop in ("auth", "auth-int"):
        return H(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return H(f"{ha1}:{nonce}:{ha2}")


def parse_auth_header(value):
    """Estrae i parametri da WWW-Authenticate/Proxy-Authenticate (Digest ...)."""
    params = {}
    # togli il prefisso 'Digest'
    v = value.strip()
    if v.lower().startswith("digest"):
        v = v[6:]
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,]+))', v):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "").strip()
        params[key] = val
    return params


def build_register(aor, server, call_id, cseq, from_tag, branch, local_ip, local_port,
                   contact_uri, auth_header=None, expires=3600):
    req_uri = f"sip:{server};transport=tls"
    lines = [
        f"REGISTER {req_uri} SIP/2.0",
        f"Via: SIP/2.0/TLS {local_ip}:{local_port};branch={branch};rport",
        f"Max-Forwards: 70",
        f"From: <sip:{aor}>;tag={from_tag}",
        f"To: <sip:{aor}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        f"Contact: <{contact_uri}>",
        f"Expires: {expires}",
        f"User-Agent: c300x-probe/1.0",
    ]
    if auth_header:
        lines.append(auth_header)
    lines.append("Content-Length: 0")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines)


def recv_response(sock, timeout=10):
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="SipAccount completo (con @...bs.iotleg.com)")
    ap.add_argument("--password", required=True, help="SipPassword")
    ap.add_argument("--cert", required=True, help="file <SipAccount>.cert.pem")
    ap.add_argument("--key", required=True, help="file <SipAccount>.key.pem")
    ap.add_argument("--cafile", help="ca-chain.cert.pem (per verificare il server)")
    ap.add_argument("--server", default=SIP_SERVER,
                    help="dominio SIP logico usato negli URI (default app.sipserver.bs.iotleg.com)")
    ap.add_argument("--connect-host",
                    help="host reale a cui aprire la connessione, dai record SRV "
                         "(es. sipserver4-n1.bs.iotleg.com). Se assente usa --server.")
    ap.add_argument("--connect-port", type=int, default=443,
                    help="porta reale dai record SRV (default 443)")
    ap.add_argument("--port", type=int, default=SIP_PORT, help="(ignorato se usi --connect-port)")
    ap.add_argument("--insecure", action="store_true",
                    help="non verificare il certificato del server (per isolare problemi di trust)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    for f in (args.cert, args.key):
        if not os.path.exists(f):
            print(f"File non trovato: {f}", file=sys.stderr)
            sys.exit(2)

    aor = args.account
    username = aor.split("@", 1)[0]       # digest username = parte locale dell'AOR

    # --- TLS context con certificato client (mutual TLS) ---
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    try:
        ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
    except ssl.SSLError as e:
        print(f"Errore caricando cert/chiave: {e}", file=sys.stderr)
        sys.exit(2)
    if args.insecure or not args.cafile:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if not args.insecure:
            print("(nessun --cafile: verifica del server disattivata)", file=sys.stderr)
    else:
        ctx.load_verify_locations(cafile=args.cafile)
        # il certificato del server potrebbe avere un CN non corrispondente all'hostname:
        # l'app usa un match custom, quindi non pretendiamo l'hostname standard
        ctx.check_hostname = False

    # host/porta REALI del trasporto (dai record SRV); il dominio logico resta args.server
    connect_host = args.connect_host or args.server
    connect_port = args.connect_port
    print(f"· Connessione TLS a {connect_host}:{connect_port}  (dominio SIP: {args.server}) …")
    try:
        raw_sock = socket.create_connection((connect_host, connect_port), timeout=15)
    except socket.gaierror:
        print(f"\nIl nome '{connect_host}' non si risolve in DNS su questa macchina.", file=sys.stderr)
        print("Trova il server reale coi record SRV e passalo con --connect-host:", file=sys.stderr)
        print("   nslookup -type=SRV _sips._tcp.app.sipserver.bs.iotleg.com", file=sys.stderr)
        print("poi usa l'host col peso più alto nella priorità più bassa, es.:", file=sys.stderr)
        print("   --connect-host sipserver4-n1.bs.iotleg.com --connect-port 443", file=sys.stderr)
        sys.exit(3)
    except OSError as e:
        print(f"\nConnessione fallita: {e}", file=sys.stderr)
        sys.exit(3)
    try:
        sock = ctx.wrap_socket(raw_sock, server_hostname=connect_host)
    except ssl.SSLError as e:
        print(f"Handshake TLS fallito: {e}", file=sys.stderr)
        print("Suggerimento: riprova con --insecure per capire se è un problema di fiducia nel "
              "certificato del server o del certificato client.", file=sys.stderr)
        sys.exit(3)

    cipher = sock.cipher()
    print(f"  TLS ok ({cipher[1]}, {cipher[0]})")
    local_ip, local_port = sock.getsockname()[:2]
    contact_uri = f"sip:{username}@{local_ip}:{local_port};transport=tls"

    call_id = uuid.uuid4().hex
    from_tag = uuid.uuid4().hex[:8]

    def new_branch():
        return "z9hG4bK" + uuid.uuid4().hex[:16]

    # --- 1) REGISTER iniziale (senza auth) ---
    cseq = 1
    msg = build_register(aor, args.server, call_id, cseq, from_tag, new_branch(),
                         local_ip, local_port, contact_uri)
    if args.verbose:
        print("\n>>> REGISTER iniziale:\n" + msg)
    sock.sendall(msg.encode())
    resp = recv_response(sock)
    if args.verbose:
        print("<<< Risposta:\n" + resp)

    status_line = resp.splitlines()[0] if resp else ""
    if " 200 " in status_line:
        print("\nREGISTRAZIONE RIUSCITA (200 OK) senza challenge. Tutto a posto!")
        sock.close()
        return

    if " 401 " not in status_line and " 407 " not in status_line:
        print(f"\nRisposta inattesa: {status_line.strip()}")
        print("Rilancia con -v e incolla la risposta (oscurando i dati sensibili).")
        sock.close()
        sys.exit(4)

    # --- 2) calcola il digest e rimanda ---
    hdr_name = "proxy-authenticate" if " 407 " in status_line else "www-authenticate"
    auth_line = None
    for line in resp.splitlines():
        if line.lower().startswith(hdr_name):
            auth_line = line.split(":", 1)[1]
            break
    if not auth_line:
        print(f"Il server ha risposto {status_line.strip()} ma senza header {hdr_name}.")
        sock.close()
        sys.exit(4)

    p = parse_auth_header(auth_line)
    realm = p.get("realm", "")
    nonce = p.get("nonce", "")
    qop = p.get("qop")
    opaque = p.get("opaque")
    if qop:
        qop = qop.split(",")[0].strip()      # a volte "auth,auth-int"
    req_uri = f"sip:{args.server};transport=tls"
    cnonce = uuid.uuid4().hex[:16]
    nc = "00000001"
    resp_digest = digest_response(username, realm, args.password, "REGISTER", req_uri,
                                  nonce, qop=qop, cnonce=cnonce, nc=nc)

    auth_parts = [
        f'Digest username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{req_uri}"',
        f'response="{resp_digest}"',
        'algorithm=MD5',
    ]
    if qop:
        auth_parts += [f'qop={qop}', f'nc={nc}', f'cnonce="{cnonce}"']
    if opaque:
        auth_parts.append(f'opaque="{opaque}"')
    auth_kind = "Proxy-Authorization" if hdr_name == "proxy-authenticate" else "Authorization"
    auth_header = f"{auth_kind}: " + ", ".join(auth_parts)

    cseq = 2
    msg2 = build_register(aor, args.server, call_id, cseq, from_tag, new_branch(),
                          local_ip, local_port, contact_uri, auth_header=auth_header)
    if args.verbose:
        print("\n>>> REGISTER con digest:\n" + msg2)
    sock.sendall(msg2.encode())
    resp2 = recv_response(sock)
    if args.verbose:
        print("<<< Risposta:\n" + resp2)

    line2 = resp2.splitlines()[0] if resp2 else ""
    if " 200 " in line2:
        print("\n==> REGISTRAZIONE RIUSCITA (200 OK). Credenziali + certificato + TLS: tutto ok!")
        print("    La catena completa funziona. Il prossimo passo è restare registrati e")
        print("    gestire la chiamata in ingresso per lo screenshot (serve un motore SIP con")
        print("    video: vedi le note in c300x_doorbell.py).")
    elif " 401 " in line2 or " 403 " in line2:
        print(f"\n==> Autenticazione RIFIUTATA: {line2.strip()}")
        print("    Password SIP errata, oppure username digest diverso dalla parte locale")
        print("    dell'AOR. Rilancia con -v e verifica realm/nonce nella risposta.")
    else:
        print(f"\n==> Risposta: {line2.strip()} (rilancia con -v per i dettagli)")
    sock.close()


# -------------------------------------------------------------------------------------
# NOTE sul passo successivo (ricezione squillo + screenshot):
#   Questo probe prova solo la REGISTER. Per ricevere l'INVITE quando suonano e catturare
#   un frame video servono negoziazione SDP, (S)RTP e decodifica H.264: non si fanno a mano
#   in modo ragionevole. Le vie pratiche:
#     - linphone (desktop o SDK) — è ciò che usa l'app; su Linux/HA è la scelta giusta.
#     - baresip (ha build Windows) con un modulo per salvare un frame.
#   Dato che l'obiettivo finale è Home Assistant (che gira su Linux), conviene spostare
#   tutta la catena lì una volta confermata la REGISTER con questo script.
# -------------------------------------------------------------------------------------

if __name__ == "__main__":
    main()
