#!/usr/bin/env python3
"""
c300x_setup.py — setup completo "chiavi in mano" per BTicino/Legrand Classe 300X (app Door Entry),
SENZA root e SENZA firmware modificato.

Fa tutto in un colpo solo:
  1) login su myhomeweb.com
  2) elenca impianti/gateway e recupera (o crea) il tuo account SIP
  3) genera chiave RSA + CSR con lo STESSO subject dell'app
  4) chiama /eliot/users/cert/signcert per farsi firmare il certificato
  5) scompatta lo ZIP restituito e salva cert + chiave + ca-chain su disco
  6) scrive un ~/.linphonerc già compilato, pronto per registrarsi al SIP
     (così poi puoi rilevare lo squillo e fare lo screenshot — vedi c300x_doorbell.py)

Tutto ciò che segue è ricostruito dall'APK Door Entry 1.17.5 (com.legrandgroup.c300x):
  * endpoint, header (prj_name: C3X, auth_token), e wrapping {"payload": ...} delle risposte
  * subject del CSR: C=IT, ST=Italy, L=Varese, O=Bticino spa, OU=DSI-PQS, CN=<SipAccount>
  * la CSR viene inviata in PEM con i newline sostituiti da spazi (replace("\n"," ")) [app: VctSecurity.f]
  * body signcert: {"CommonName": <SipAccount>, "CSR": <csr>}
  * la risposta è uno ZIP (base64 nel campo payload) con <SipAccount>.cert.pem e ca-chain.cert.pem
  * dominio/realm SIP: <GatewayId>.bs.iotleg.com, transport TLS/5061, le chiamate arrivano da c300x@...

Dipendenze:  pip install cryptography   (per generare la CSR)
             la libreria standard basta per HTTP, base64 e zip.

USO:
  python3 c300x_setup.py <email> <password>
  python3 c300x_setup.py <email> <password> --out ./c300x-certs -v

NOTA ETICA/LEGALE: usa solo con il TUO account e il TUO citofono. Nell'UE il reverse engineering
per interoperabilità è generalmente lecito, ma può violare i ToS dell'app: valuta tu.
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.request
import urllib.error
import zipfile

PRJ_NAME = "C3X"
APP_VERSION = "legacy-1.17.5"
PORTAL = "https://www.myhomeweb.com"
SIP_SUFFIX = ".bs.iotleg.com"
SIP_SERVER = "app.sipserver.bs.iotleg.com"   # registrar SIP di produzione (VctSettings.D1)

# Subject del CSR, identico all'app (solo il CN cambia col tuo SipAccount)
CSR_SUBJECT = {
    "C": "IT", "ST": "Italy", "L": "Varese",
    "O": "Bticino spa", "OU": "DSI-PQS",
}


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self, base=PORTAL, verbose=False):
        self.base = base.rstrip("/")
        self.token = None
        self.verbose = verbose

    def _req(self, method, path, *, body=None, headers=None, raw_bytes=False):
        url = self.base + path
        h = {"prj_name": PRJ_NAME}
        if self.token:
            h["auth_token"] = self.token
        if body is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        if self.verbose:
            print(f">>> {method} {url}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                blob = r.read()
                nt = r.headers.get("auth_token")
                if nt:
                    self.token = nt
                if self.verbose:
                    print(f"<<< {r.status} (len {len(blob)})", file=sys.stderr)
                # signcert restituisce byte binari (ZIP): non decodificarli come testo
                return blob if raw_bytes else blob.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise PortalError(f"{method} {path} -> HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
        except urllib.error.URLError as e:
            raise PortalError(f"{method} {path} -> rete: {e.reason}")

    @staticmethod
    def payload(raw):
        if not raw.strip():
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(obj, dict) and isinstance(obj.get("payload"), str):
            try:
                return json.loads(obj["payload"])
            except json.JSONDecodeError:
                return obj["payload"]
        return obj

    def sign_in(self, user, pwd):
        body = json.dumps({"username": user, "pwd": pwd, "appVersion": APP_VERSION})
        raw = self._req("POST", "/eliot/users/sign_in", body=body)
        if not self.token:
            d = self.payload(raw)
            if isinstance(d, dict):
                self.token = d.get("auth_token") or d.get("Token")
        if not self.token:
            raise PortalError("login ok ma nessun auth_token. Risposta: " + raw[:200])
        return self.payload(raw)

    def plants(self):
        d = self.payload(self._req("GET", "/eliot/plants"))
        return d if isinstance(d, list) else [d]

    def gateways(self, plant_id):
        d = self.payload(self._req("GET", f"/eliot/plants/{plant_id}/gateway/"))
        return d if isinstance(d, list) else [d]

    def sip_users(self, plant_id, gw_id):
        d = self.payload(self._req("GET", f"/eliot/sip/users/plants/{plant_id}/gateway/{gw_id}"))
        return d if isinstance(d, list) else [d]

    def signcert(self, common_name, csr_pem_oneline):
        body = json.dumps({"CommonName": common_name, "CSR": csr_pem_oneline})
        # la risposta è lo ZIP binario coi .pem: va letta come byte, non come testo
        return self._req("POST", "/eliot/users/cert/signcert", body=body, raw_bytes=True)


def _get(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d:
                return d[k]
    return default


def make_csr(common_name):
    """Genera (private_key_pem, csr_pem_oneline) con lo stesso subject dell'app."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("Serve 'cryptography':  pip install cryptography", file=sys.stderr)
        raise

    import warnings
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # silenzia l'avviso sul CN > 64 char (voluto)
        _cn = x509.NameAttribute(NameOID.COMMON_NAME, common_name, _validate=False)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, CSR_SUBJECT["C"]),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, CSR_SUBJECT["ST"]),
        x509.NameAttribute(NameOID.LOCALITY_NAME, CSR_SUBJECT["L"]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CSR_SUBJECT["O"]),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, CSR_SUBJECT["OU"]),
        # Il SipAccount supera i 64 caratteri del limite X.509 standard; l'app usa Java/BouncyCastle
        # che non lo applica, quindi disattiviamo la validazione per riprodurre lo stesso CSR.
        _cn,
    ])
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(name)
           .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    # l'app sostituisce i newline con spazi prima di inviare
    csr_oneline = csr_pem.replace("\n", " ")
    return key_pem, csr_oneline


def unpack_cert_zip(raw_response, out_dir, sip_account):
    """Scompatta l'archivio dei certificati restituito da signcert.
    Il server restituisce direttamente i byte di uno ZIP (inizia con 'PK'); gestiamo anche
    il caso in cui fosse incapsulato in JSON {"payload": "<base64>"} come altre risposte."""
    # normalizza in byte
    if isinstance(raw_response, str):
        data_bytes = raw_response.encode("latin-1", "replace")
    else:
        data_bytes = raw_response

    blob = None
    # 1) è già uno ZIP? (magic 'PK\x03\x04' o 'PK\x05\x06' per zip vuoto)
    if data_bytes[:2] == b"PK":
        blob = data_bytes
    else:
        # 2) prova come JSON {"payload": "<base64>"}
        try:
            obj = json.loads(data_bytes.decode("utf-8", "replace"))
            if isinstance(obj, dict) and isinstance(obj.get("payload"), str):
                blob = base64.b64decode(obj["payload"])
        except (json.JSONDecodeError, ValueError):
            pass
        # 3) prova come base64 "nudo"
        if blob is None:
            try:
                blob = base64.b64decode(data_bytes, validate=False)
                if blob[:2] != b"PK":
                    blob = None
            except Exception:
                blob = None

    if blob is None or blob[:2] != b"PK":
        head = data_bytes[:80]
        raise PortalError(
            "la risposta di signcert non è uno ZIP riconoscibile. "
            f"Primi byte: {head!r}"
        )

    saved = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            data = z.read(name)
            base = os.path.basename(name)
            path = os.path.join(out_dir, base)
            with open(path, "wb") as f:
                f.write(data)
            saved[base] = path
    return saved


def write_linphonerc(out_dir, sip_account, sip_password, gateway_id,
                     cert_path, key_path, cachain_path):
    username = sip_account.split("@", 1)[0]
    domain = f"{gateway_id}{SIP_SUFFIX}"
    # IMPORTANTE: ci si registra al server SIP di Legrand (app.sipserver.bs.iotleg.com),
    # NON al dominio del gateway. Il dominio del gateway fa solo da host dell'identità.
    rc = f"""# ~/.linphonerc generato da c300x_setup.py — Classe 300X
[sip]
contact=sip:{sip_account}
sip_tcp_port=0
sip_udp_port=0
sip_tls_port=5061
media_encryption=srtp
root_ca={cachain_path}
client_cert_chain={cert_path}
client_cert_key={key_path}
verify_server_certs=1
tls_certificate_subject_regexp=(sip:{domain}|{SIP_SERVER}|(sipserver|sipstaging).bs.iotleg.com)

[auth_info_0]
username={username}
passwd={sip_password}
realm=

[proxy_0]
reg_proxy=<sip:{SIP_SERVER};transport=tls>
reg_identity=sip:{sip_account}
reg_sendregister=1
reg_expires=5184000

[video]
display=0
capture=0
# Il citofono chiama da sip:c300x@{domain}
# Dopo 'answer', usa 'snapshot /tmp/doorbell.jpg' per catturare un frame.
"""
    path = os.path.join(out_dir, "linphonerc")
    with open(path, "w") as f:
        f.write(rc)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", help="email account Door Entry")
    ap.add_argument("password", help="password account Door Entry")
    ap.add_argument("--base", default=PORTAL)
    ap.add_argument("--out", default="./c300x-certs", help="cartella di output")
    ap.add_argument("--plant", help="PlantId specifico (se ne hai più d'uno)")
    ap.add_argument("--account", help="scegli l'utenza SIP che contiene questa stringa "
                                      "(es. l'IdDevice del nuovo interno creato con add-device)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    p = Portal(base=args.base, verbose=args.verbose)

    print("· Login…")
    p.sign_in(args.username, args.password)
    print("  ok.\n")

    print("· Cerco impianti e gateway…")
    plants = p.plants()
    target = None
    for pl in plants:
        pid = _get(pl, "PlantId", "plantId", "Id")
        if args.plant and str(pid) != str(args.plant):
            continue
        pname = _get(pl, "PlantName", default="(senza nome)")
        gws = []
        try:
            gws = p.gateways(pid)
        except PortalError:
            pass
        gw_ids = [_get(g, "GatewayId", "gatewayId") for g in gws]
        gw_ids = [g for g in gw_ids if g] or [_get(pl, "GatewayId")]
        for gwid in gw_ids:
            if not gwid:
                continue
            try:
                sips = p.sip_users(pid, gwid)
            except PortalError as e:
                print(f"  ! sip_users({pid},{gwid}): {e}")
                continue
            if sips:
                # se --account è dato, scegli l'utenza che corrisponde (match su SipAccount);
                # altrimenti la prima
                s = sips[0]
                if args.account:
                    match = next((x for x in sips
                                  if args.account.lower() in str(_get(x, "SipAccount", "sipAccount", default="")).lower()),
                                 None)
                    if match:
                        s = match
                    else:
                        print(f"  ! nessuna utenza corrisponde a --account '{args.account}' su questo gateway")
                        continue
                target = {
                    "plant_id": pid, "plant_name": pname, "gateway_id": gwid,
                    "sip_account": _get(s, "SipAccount", "sipAccount"),
                    "sip_password": _get(s, "SipPassword", "sipPassword"),
                    "username": _get(s, "Username", "username"),
                    "id_device": _get(s, "IdDevice", "idDevice"),
                }
                print(f"  impianto '{pname}' / gateway {gwid}: trovato SIP account.")
                break
        if target:
            break

    if not target or not target["sip_account"]:
        print("Nessun account SIP trovato. Possibili cause: l'account SIP per questo\n"
              "dispositivo non è ancora stato creato (nell'app lo crea al primo avvio),\n"
              "oppure il backend usa nomi di campo diversi. Rilancia con -v e incolla l'output.",
              file=sys.stderr)
        sys.exit(2)

    sip_account = target["sip_account"]
    print(f"\n· Genero chiave RSA 2048 + CSR (CN={sip_account})…")
    key_pem, csr_oneline = make_csr(sip_account)
    key_path = os.path.join(args.out, f"{sip_account}.key.pem")
    with open(key_path, "w") as f:
        f.write(key_pem)
    print(f"  chiave privata salvata: {key_path}")

    print("· Richiedo la firma del certificato (/eliot/users/cert/signcert)…")
    raw = p.signcert(sip_account, csr_oneline)

    print("· Scompatto l'archivio restituito…")
    saved = unpack_cert_zip(raw, args.out, sip_account)
    if not saved:
        print("  L'archivio è vuoto o non è uno ZIP. Rilancia con -v e incolla l'output.",
              file=sys.stderr)
        sys.exit(3)
    for name, path in saved.items():
        print(f"  {name} -> {path}")

    # individua cert e ca-chain con nomi robusti
    cert_path = next((v for k, v in saved.items() if k.endswith(".cert.pem") and "ca-chain" not in k), None)
    cachain_path = next((v for k, v in saved.items() if "ca-chain" in k), None)
    cert_path = cert_path or next(iter(saved.values()))
    cachain_path = cachain_path or cert_path

    rc_path = write_linphonerc(
        args.out, sip_account, target["sip_password"], target["gateway_id"],
        cert_path, key_path, cachain_path,
    )
    print(f"\n· Scritto {rc_path}")
    print("\nFatto. Prova la catena completa con:")
    print(f"   linphonec -c {rc_path}")
    print("   # attendi 'Registration on ... successful'")
    print("   # quando suonano vedrai 'Incoming call', poi:")
    print("   #   answer")
    print("   #   snapshot /tmp/doorbell.jpg")
    print("   #   terminate")
    print("\nSuggerimento: tieni al sicuro la chiave privata (.key.pem); senza quella il")
    print("certificato non serve a nessuno, ma con quella chiunque può registrarsi come te.")


if __name__ == "__main__":
    main()
