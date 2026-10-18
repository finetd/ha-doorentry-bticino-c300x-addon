#!/usr/bin/env python3
"""
c300x_client.py — prototipo di client per BTicino / Legrand Classe 300X (app "Door Entry").

Ricostruito via reverse engineering dell'APK Door Entry 1.17.5 (package com.legrandgroup.c300x).
NON usa root né firmware modificato: replica esattamente ciò che fa l'app ufficiale.

ATTENZIONE — stato del progetto:
  * Lo STEP 1 (login + scoperta impianto/gateway + credenziali SIP) è completo e testabile da solo.
    È il primo pezzo da verificare col tuo account: se funziona, il resto viene a cascata.
  * Lo STEP 2 (registrazione SIP su TLS + firma certificato + invio del comando di apertura porta)
    è abbozzato e documentato ma richiede una libreria SIP esterna. Le costanti e il formato
    dei frame OpenWebNet sono quelli reali estratti dall'app.

Mappa del protocollo (tutto ciò che segue è stato letto dal bytecode, non inventato):

  Portale REST (produzione):  https://www.myhomeweb.com   (porta 443)
  Header comuni su ogni chiamata:  prj_name: C3X   e, dopo il login,  auth_token: <token>
  Il token arriva nell'header di risposta "auth_token" e va rimesso su ogni richiesta successiva.

  POST /eliot/users/sign_in
       body JSON (raw): {"username", "pwd", "appVersion":"legacy-1.17.5", "registrationId"(opz.)}
       -> risposta: payload JSON + header auth_token
  GET  /eliot/plants
       -> lista impianti; ogni elemento ha PlantId, PlantName, GatewayId...
  GET  /eliot/plants/{plantId}/gateway/
       -> lista gateway; ogni elemento ha GatewayId
  GET  /eliot/sip/users/plants/{plantId}/gateway/{gatewayId}
       -> lista "SIP user": {SipAccount, Username, SipPassword, GatewayId, IdDevice}
  POST /eliot/sip/user      (crea il proprio account SIP, se non esiste — payload da C0926b.o())
  POST /eliot/users/cert/signcert   body {"CommonName", "CSR"} -> restituisce uno ZIP base64
       contenente <SipAccount>.cert.pem + ca-chain.cert.pem  (certificato client per il SIP su TLS)

  SIP (via server Legrand, che fa da ponte verso il citofono):
     server/dominio di registrazione:  <GatewayId>.bs.iotleg.com   (transport=tls, porta 5061)
       [server SIP di default alternativo: app.sipserver.bs.iotleg.com]
     identity:  sip:<SipAccount>            (SipAccount ha già forma  utente-mac@<gwid>.bs.iotleg.com)
     auth:      username = parte locale di SipAccount,  password = SipPassword,  realm = dominio
     I comandi NON sono DTMF: sono messaggi SIP (SIP MESSAGE / chat) il cui corpo è un frame
     OpenWebNet, inviati a  sip:c300x@<GatewayId>.bs.iotleg.com

  Comandi OpenWebNet reali (da VctLinphoneService.P / .V):
     Apertura serratura (CID 10060 o 3008):   invia DUE messaggi:  *8*19*<PE>##  poi  *8*20*<PE>##
     Apertura serratura (CID 2009):            *8*21*<PE>##  poi  *8*22*<PE>##   (<PE> vuoto -> "4")
     Attivazione generica (luce scale ecc.):   *13*35*##
     Segreteria on/off:                        *8*92##  /  *8*91##
     <PE> = indirizzo del dispositivo serratura: device.g() concatenato a device.d()
            (se g() è null, vale "2"). Per la serratura "di sessione" (nessun device) l'app usa CID 10060.

Per lo STEP 2 il consiglio è di NON reimplementare SIP+TLS+SRTP a mano, ma di pilotare linphone
(libreskit / python-linphone) oppure PJSIP, che l'app stessa usa (è un fork di linphone).
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

APP_VERSION = "legacy-1.17.5"          # campo appVersion nel login (prefisso "legacy-" + versione app)
PRJ_NAME = "C3X"                       # header prj_name richiesto su OGNI chiamata
PORTAL_PRODUCTION = "https://www.myhomeweb.com"
SIP_DOMAIN_SUFFIX = ".bs.iotleg.com"   # dominio SIP = <GatewayId> + questo suffisso
SIP_PEER = "c300x"                     # i comandi vanno a sip:c300x@<dominio>

# Frame OpenWebNet per l'apertura porta (coppia apri+rilascia), presi da VctLinphoneService.P()
def door_open_frames(pe_address: str, cid: int = 10060):
    """Ritorna la coppia di frame OpenWebNet da inviare per aprire una serratura.
    pe_address: l'indirizzo PE della serratura (es. '2' + indirizzo, vedi device.g()+device.d()).
    cid: Category ID del dispositivo; 10060/3008 usano *8*19/*8*20, 2009 usa *8*21/*8*22.
    """
    pe = pe_address if pe_address else "4"
    if cid == 2009:
        return (f"*8*21*{pe}##", f"*8*22*{pe}##")
    # default: serratura standard
    return (f"*8*19*{pe}##", f"*8*20*{pe}##")

STAIRCASE_LIGHT_FRAME = "*13*35*##"    # attivazione generica (luce scale)


class PortalError(RuntimeError):
    pass


class C300XPortal:
    """Client REST per il portale myhomeweb (parte cloud dell'app Door Entry)."""

    def __init__(self, base_url: str = PORTAL_PRODUCTION, verbose: bool = False):
        self.base = base_url.rstrip("/")
        self.token = None
        self.verbose = verbose

    # --- HTTP di basso livello -------------------------------------------------
    def _request(self, method: str, path: str, *, body: str = None, extra_headers=None):
        url = self.base + path
        headers = {"prj_name": PRJ_NAME}
        if self.token:
            headers["auth_token"] = self.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if self.verbose:
            print(f">>> {method} {url}", file=sys.stderr)
            if body:
                print(f"    body: {body}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
                # il portale rinnova il token negli header di risposta
                new_token = resp.headers.get("auth_token")
                if new_token:
                    self.token = new_token
                if self.verbose:
                    print(f"<<< {resp.status}  (len {len(raw)})", file=sys.stderr)
                return resp.status, resp.headers, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise PortalError(f"{method} {path} -> HTTP {e.code}: {raw[:300]}") from None
        except urllib.error.URLError as e:
            raise PortalError(f"{method} {path} -> errore di rete: {e.reason}") from None

    @staticmethod
    def _payload(raw: str):
        """Le risposte wrappano il contenuto in {'payload': '<json-stringificato>'} oppure
        restituiscono direttamente il JSON. Gestiamo entrambi i casi."""
        if not raw.strip():
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(obj, dict) and "payload" in obj and isinstance(obj["payload"], str):
            try:
                return json.loads(obj["payload"])
            except json.JSONDecodeError:
                return obj["payload"]
        return obj

    # --- API ad alto livello ---------------------------------------------------
    def sign_in(self, username: str, password: str, registration_id: str = None):
        """POST /eliot/users/sign_in — autentica e memorizza il token."""
        body = {"username": username, "pwd": password, "appVersion": APP_VERSION}
        if registration_id:
            body["registrationId"] = registration_id
        status, headers, raw = self._request(
            "POST", "/eliot/users/sign_in", body=json.dumps(body)
        )
        if not self.token:
            # fallback: alcuni backend mettono il token nel corpo
            data = self._payload(raw)
            if isinstance(data, dict):
                self.token = data.get("auth_token") or data.get("Token")
        if not self.token:
            raise PortalError(
                "login riuscito ma nessun auth_token trovato (controlla header/response): "
                + raw[:300]
            )
        return self._payload(raw)

    def plants(self):
        """GET /eliot/plants — lista impianti."""
        _, _, raw = self._request("GET", "/eliot/plants")
        data = self._payload(raw)
        return data if isinstance(data, list) else [data]

    def gateways(self, plant_id):
        """GET /eliot/plants/{id}/gateway/ — lista gateway dell'impianto."""
        _, _, raw = self._request("GET", f"/eliot/plants/{plant_id}/gateway/")
        data = self._payload(raw)
        return data if isinstance(data, list) else [data]

    def sip_users(self, plant_id, gateway_id):
        """GET /eliot/sip/users/plants/{plant}/gateway/{gw} — credenziali SIP.
        Ogni elemento: SipAccount, Username, SipPassword, GatewayId, IdDevice."""
        _, _, raw = self._request(
            "GET", f"/eliot/sip/users/plants/{plant_id}/gateway/{gateway_id}"
        )
        data = self._payload(raw)
        return data if isinstance(data, list) else [data]

    def create_sip_user(self, sip_account, device_name, gateway_id, id_device):
        """POST /eliot/sip/user — crea un nuovo interno (handset) sull'impianto.
        Corpo come l'app (C3xSipUser.o): SipAccount, DeviceName, GatewayId, IdDevice."""
        body = json.dumps({
            "SipAccount": sip_account,
            "DeviceName": device_name,
            "GatewayId": gateway_id,
            "IdDevice": id_device,
        })
        status, headers, raw = self._request("POST", "/eliot/sip/user", body=body)
        return self._payload(raw)

    def conf_archive(self, plant_id, gateway_id):
        """GET /eliot/plants/{plant}/gateway/{gw}/conf — archivio ZIP con conf.xml ecc.
        Ritorna i byte grezzi (contenuto binario, non testo)."""
        url = self.base + f"/eliot/plants/{plant_id}/gateway/{gateway_id}/conf"
        headers = {"prj_name": PRJ_NAME}
        if self.token:
            headers["auth_token"] = self.token
        req = urllib.request.Request(url, method="GET", headers=headers)
        if self.verbose:
            print(f">>> GET {url}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                blob = resp.read()
                if self.verbose:
                    print(f"<<< {resp.status} (len {len(blob)} bytes)", file=sys.stderr)
                return blob
        except urllib.error.HTTPError as e:
            raise PortalError(f"GET conf -> HTTP {e.code}: {e.read()[:200]!r}") from None
        except urllib.error.URLError as e:
            raise PortalError(f"GET conf -> errore di rete: {e.reason}") from None


def _get(d, *keys, default=None):
    """Prende la prima chiave presente (gestisce varianti di maiuscole dal backend)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d:
            return d[k]
    return default


def cmd_discover(args):
    portal = C300XPortal(base_url=args.base, verbose=args.verbose)
    print("· Login…")
    portal.sign_in(args.username, args.password)
    print(f"  ok, token: {portal.token[:12]}…\n")

    plants = portal.plants()
    print(f"· Impianti trovati: {len(plants)}")
    for p in plants:
        pid = _get(p, "PlantId", "plantId", "Id")
        pname = _get(p, "PlantName", "plantName", default="(senza nome)")
        pgw = _get(p, "GatewayId", "gatewayId")
        print(f"  - {pname}   PlantId={pid}   GatewayId={pgw}")

        gws = []
        if pid is not None:
            try:
                gws = portal.gateways(pid)
            except PortalError as e:
                print(f"      (gateway: {e})")
        gw_ids = [_get(g, "GatewayId", "gatewayId") for g in gws] or ([pgw] if pgw else [])
        for gwid in gw_ids:
            if not gwid:
                continue
            print(f"      gateway {gwid}  ->  dominio SIP: {gwid}{SIP_DOMAIN_SUFFIX}")
            try:
                sips = portal.sip_users(pid, gwid)
            except PortalError as e:
                print(f"        (sip users: {e})")
                continue
            print(f"        --- {len(sips)} utenze SIP (interni) su questo gateway ---")
            for i, s in enumerate(sips, 1):
                acc = _get(s, "SipAccount", "sipAccount")
                usr = _get(s, "Username", "username")
                pwd = _get(s, "SipPassword", "sipPassword")
                idd = _get(s, "IdDevice", "idDevice")
                name = _get(s, "DeviceName", "deviceName", "Name", "name", default="(nessun nome)")
                pwd_show = (pwd[:3] + "…") if pwd else None
                print(f"        [{i}] DeviceName : {name}")
                print(f"            SipAccount: {acc}")
                print(f"            user      : {usr}")
                print(f"            pass      : {pwd_show}   (--show-secrets per intera)")
                print(f"            IdDevice  : {idd}")
                # stampa eventuali altri campi non previsti, utili a capire il tipo di interno
                if isinstance(s, dict):
                    extra = {k: v for k, v in s.items()
                             if k not in ("SipAccount", "sipAccount", "Username", "username",
                                          "SipPassword", "sipPassword", "IdDevice", "idDevice",
                                          "DeviceName", "deviceName", "Name", "name")}
                    if extra:
                        print(f"            altri     : {extra}")
                if args.show_secrets:
                    print(f"            [FULL] password: {pwd}")
    print(
        "\nProssimo passo: registrarsi al SIP come\n"
        "   identity sip:<SipAccount> , auth user=<parte prima di @ in Username/SipAccount>,\n"
        "   password=<SipPassword>, dominio/realm=<GatewayId>.bs.iotleg.com, transport=TLS (5061),\n"
        "   poi inviare un SIP MESSAGE con corpo '*8*19*<PE>##' seguito da '*8*20*<PE>##'\n"
        "   a sip:c300x@<GatewayId>.bs.iotleg.com per aprire la porta.\n"
        "Per il TLS serve prima POST /eliot/users/cert/signcert (CSR) — vedi i commenti in testa."
    )


def cmd_frames(args):
    opn, rel = door_open_frames(args.pe, cid=args.cid)
    print("Frame OpenWebNet per aprire la serratura (inviali in sequenza come due SIP MESSAGE):")
    print(f"  1) {opn}")
    print(f"  2) {rel}")
    print(f"Destinatario SIP: sip:{SIP_PEER}@<GatewayId>{SIP_DOMAIN_SUFFIX}")
    print(f"Luce scale / attivazione generica: {STAIRCASE_LIGHT_FRAME}")


def cmd_add_device(args):
    import uuid as _uuid
    portal = C300XPortal(base_url=args.base, verbose=args.verbose)
    print("· Login…")
    portal.sign_in(args.username, args.password)
    print("  ok.\n")

    plants = portal.plants()
    pl = None
    for p in plants:
        pid = _get(p, "PlantId", "plantId", "Id")
        if not args.plant or str(pid) == str(args.plant):
            pl = p
            break
    if not pl:
        print("Impianto non trovato."); return
    pid = _get(pl, "PlantId", "plantId", "Id")
    gws = []
    try:
        gws = portal.gateways(pid)
    except PortalError:
        pass
    gwid = next((_get(g, "GatewayId", "gatewayId") for g in gws if _get(g, "GatewayId", "gatewayId")),
                _get(pl, "GatewayId"))
    if not gwid:
        print("GatewayId non trovato."); return

    # IdDevice NUOVO e unico (qui sta la differenza: non riusare quello del telefono!)
    id_device = str(_uuid.uuid4()).upper()
    email_part = args.username.replace("@", "-")
    sip_account = f"{email_part}-{id_device}@{gwid}.bs.iotleg.com"
    device_name = args.name

    print(f"· Creo un nuovo interno sull'impianto (gateway {gwid}):")
    print(f"    DeviceName: {device_name}")
    print(f"    IdDevice  : {id_device}  (nuovo, univoco)")
    print(f"    SipAccount: {sip_account}")
    try:
        portal.create_sip_user(sip_account, device_name, gwid, id_device)
    except PortalError as e:
        print(f"  ERRORE nella creazione: {e}"); return
    print("  creato. Rileggo le utenze per ottenere la password…\n")

    # rileggi e trova la nostra nuova utenza (per IdDevice)
    found = None
    try:
        for s in portal.sip_users(pid, gwid):
            if _get(s, "IdDevice", "idDevice") == id_device:
                found = s
                break
    except PortalError as e:
        print(f"  (rilettura utenze: {e})")
    if not found:
        print("  La nuova utenza non è ancora comparsa nell'elenco. Rilancia 'discover' tra poco.")
        return
    acc = _get(found, "SipAccount", "sipAccount")
    pwd = _get(found, "SipPassword", "sipPassword")
    print("  === NUOVO INTERNO CREATO ===")
    print(f"  SipAccount : {acc}")
    print(f"  SipPassword: {pwd}")
    print(f"  IdDevice   : {id_device}")
    print("\nProssimi passi:")
    print(f"  1) Genera i certificati per QUESTO account:")
    print(f"       python c300x_setup.py \"{args.username}\" \"<password-login>\" --out .\\c300x-certs --account \"{acc}\"")
    print(f"  2) Configura l'add-on con questo SipAccount e SipPassword.")
    print(f"  3) Fai suonare il citofono: ora dovrebbe arrivare CALL_INCOMING.")


def cmd_conf(args):
    import base64 as _b64, io as _io, os as _os, zipfile as _zip
    import xml.etree.ElementTree as _ET
    portal = C300XPortal(base_url=args.base, verbose=args.verbose)
    print("· Login…")
    portal.sign_in(args.username, args.password)
    print("  ok.\n")

    plants = portal.plants()
    for pl in plants:
        pid = _get(pl, "PlantId", "plantId", "Id")
        if args.plant and str(pid) != str(args.plant):
            continue
        pname = _get(pl, "PlantName", default="(senza nome)")
        gws = []
        try:
            gws = portal.gateways(pid)
        except PortalError:
            pass
        gw_ids = [_get(g, "GatewayId", "gatewayId") for g in gws]
        gw_ids = [g for g in gw_ids if g] or [_get(pl, "GatewayId")]
        for gwid in gw_ids:
            if not gwid:
                continue
            print(f"· Impianto '{pname}' / gateway {gwid}: scarico la configurazione…")
            try:
                blob = portal.conf_archive(pid, gwid)
            except PortalError as e:
                print(f"  errore: {e}"); continue

            # la risposta è uno ZIP grezzo (inizia con 'PK'); gestiamo anche base64/json
            if blob[:2] != b"PK":
                try:
                    obj = json.loads(blob.decode("utf-8", "replace"))
                    if isinstance(obj, dict) and isinstance(obj.get("payload"), str):
                        blob = _b64.b64decode(obj["payload"])
                except Exception:
                    try:
                        blob = _b64.b64decode(blob)
                    except Exception:
                        pass
            if blob[:2] != b"PK":
                print(f"  la risposta non è uno ZIP. Primi byte: {blob[:60]!r}"); continue

            # l'archivio è cifrato con password fissa dell'app (da K2/h.java)
            ZIP_PASSWORD = b"mhpG_123!"
            files = {}
            try:
                # prima prova con zipfile standard (ZipCrypto legacy)
                with _zip.ZipFile(_io.BytesIO(blob)) as z:
                    for name in z.namelist():
                        files[_os.path.basename(name)] = z.read(name, pwd=ZIP_PASSWORD)
            except (RuntimeError, NotImplementedError):
                # cifratura AES: serve pyzipper
                try:
                    import pyzipper
                except ImportError:
                    print("  L'archivio usa cifratura AES. Installa pyzipper:")
                    print("     pip install pyzipper")
                    print("  poi rilancia questo stesso comando.")
                    continue
                files = {}
                with pyzipper.AESZipFile(_io.BytesIO(blob)) as z:
                    z.setpassword(ZIP_PASSWORD)
                    for name in z.namelist():
                        files[_os.path.basename(name)] = z.read(name)
            print(f"  archivio: {', '.join(sorted(files))}")

            if args.save_conf:
                _os.makedirs(args.save_conf, exist_ok=True)
                for name, data in files.items():
                    with open(_os.path.join(args.save_conf, name), "wb") as fh:
                        fh.write(data)
                print(f"  file salvati in {args.save_conf}")

            if "conf.xml" not in files:
                print("  conf.xml assente nell'archivio."); continue
            try:
                root = _ET.fromstring(files["conf.xml"])
            except _ET.ParseError as e:
                print(f"  conf.xml non valido: {e}"); continue

            dev = addr = None
            for comm in root.iter("communication"):
                pd = comm.find("p_default")
                if pd is not None:
                    dev = pd.findtext("dev") or dev
                    addr = pd.findtext("address") or addr
            print("\n  === RISULTATO ===")
            print(f"  p_default/dev     = {dev}")
            print(f"  p_default/address = {addr}")
            if dev is not None and addr is not None:
                print(f"\n  >>> DEVADDR (posto esterno di default) = {dev}{addr}")
            else:
                print("\n  DEVADDR non ricavato: apri conf.xml (con --save-conf) e mandami il")
                print("  blocco <vdes><communication>.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=PORTAL_PRODUCTION,
                    help=f"URL portale (default {PORTAL_PRODUCTION})")
    ap.add_argument("-v", "--verbose", action="store_true", help="log delle richieste HTTP")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover",
                       help="login + elenco impianti/gateway + credenziali SIP (STEP 1, testabile)")
    d.add_argument("username", help="email dell'account Door Entry")
    d.add_argument("password", help="password dell'account Door Entry")
    d.add_argument("--show-secrets", action="store_true",
                   help="stampa la SipPassword in chiaro")
    d.set_defaults(func=cmd_discover)

    f = sub.add_parser("frames", help="stampa i frame OpenWebNet di apertura porta (offline)")
    f.add_argument("--pe", default="", help="indirizzo PE della serratura (device.g()+device.d())")
    f.add_argument("--cid", type=int, default=10060, help="Category ID (10060/3008 default, o 2009)")
    f.set_defaults(func=cmd_frames)

    a = sub.add_parser("add-device", help="crea un nuovo interno SIP (handset) che il citofono farà squillare")
    a.add_argument("username", help="email dell'account Door Entry")
    a.add_argument("password", help="password dell'account Door Entry")
    a.add_argument("--name", default="Home Assistant", help="nome del nuovo dispositivo")
    a.add_argument("--plant", help="PlantId specifico")
    a.set_defaults(func=cmd_add_device)

    c = sub.add_parser("conf", help="scarica la configurazione impianto e stampa il DEVADDR")
    c.add_argument("username", help="email dell'account Door Entry")
    c.add_argument("password", help="password dell'account Door Entry")
    c.add_argument("--plant", help="PlantId specifico (se hai più impianti)")
    c.add_argument("--save-conf", help="cartella dove salvare conf.xml e gli altri file")
    c.set_defaults(func=cmd_conf)

    args = ap.parse_args()
    try:
        args.func(args)
    except PortalError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
