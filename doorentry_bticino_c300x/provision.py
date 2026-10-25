#!/usr/bin/env python3
"""
provision.py — provisioning automatico per l'add-on BTicino C300X Doorbell.

Esegue, a partire da SOLO email + password dell'account Door Entry, tutti i passaggi che
prima erano manuali:
  1) login su myhomeweb
  2) trova impianto e gateway
  3) crea (una volta sola) un interno SIP dedicato con IdDevice UNICO -> così il citofono lo
     fa squillare come dispositivo a sé (persiste in /data/state.json e lo riusa ai riavvii)
  4) recupera la SipPassword dell'interno
  5) genera chiave + CSR e fa firmare il certificato client (/eliot/users/cert/signcert)
  6) (best-effort) scarica conf.xml e ricava il DEVADDR del posto esterno
  7) scrive i file per baresip: /etc/baresip/config, accounts, client.pem
     e /data/runtime.json con i valori che servono a doorbell.py

Rilanciabile a ogni avvio: se stato e certificati esistono già, li riusa.
"""

import base64
import glob
import io
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET
import zipfile

PRJ_NAME = "C3X"
APP_VERSION = "legacy-1.17.5"
PORTAL = "https://www.myhomeweb.com"
SIP_SERVER = "app.sipserver.bs.iotleg.com"
SIP_SUFFIX = ".bs.iotleg.com"
ZIP_PASSWORD = b"mhpG_123!"
CSR_SUBJECT = {"C": "IT", "ST": "Italy", "L": "Varese", "O": "Bticino spa", "OU": "DSI-PQS"}

DATA_DIR = "/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CERT_DIR = os.path.join(DATA_DIR, "certs")
RUNTIME_FILE = os.path.join(DATA_DIR, "runtime.json")
BARESIP_DIR = "/etc/baresip"

EMAIL = os.environ.get("EMAIL", "").strip()
PASSWORD = os.environ.get("PASSWORD", "")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Home Assistant")
MODDIR = os.environ.get("MODDIR", "/usr/lib/baresip/modules")


def log(*a):
    print("[provision]", *a, flush=True)


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self, base=PORTAL):
        self.base = base.rstrip("/")
        self.token = None

    def _req(self, method, path, *, body=None, raw_bytes=False):
        url = self.base + path
        h = {"prj_name": PRJ_NAME}
        if self.token:
            h["auth_token"] = self.token
        if body is not None:
            h["Content-Type"] = "application/json"
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                blob = r.read()
                nt = r.headers.get("auth_token")
                if nt:
                    self.token = nt
                return blob if raw_bytes else blob.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise PortalError(f"{method} {path} -> HTTP {e.code}: {e.read()[:200]!r}")
        except urllib.error.URLError as e:
            raise PortalError(f"{method} {path} -> rete: {e.reason}")

    @staticmethod
    def payload(raw):
        if not raw or not str(raw).strip():
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
            raise PortalError("login ok ma nessun auth_token")

    def plants(self):
        d = self.payload(self._req("GET", "/eliot/plants"))
        return d if isinstance(d, list) else [d]

    def gateways(self, pid):
        d = self.payload(self._req("GET", f"/eliot/plants/{pid}/gateway/"))
        return d if isinstance(d, list) else [d]

    def sip_users(self, pid, gw):
        d = self.payload(self._req("GET", f"/eliot/sip/users/plants/{pid}/gateway/{gw}"))
        return d if isinstance(d, list) else [d]

    def create_sip_user(self, sip_account, device_name, gw, id_device):
        body = json.dumps({"SipAccount": sip_account, "DeviceName": device_name,
                           "GatewayId": gw, "IdDevice": id_device})
        return self._req("POST", "/eliot/sip/user", body=body)

    def signcert(self, common_name, csr_oneline):
        body = json.dumps({"CommonName": common_name, "CSR": csr_oneline})
        return self._req("POST", "/eliot/users/cert/signcert", body=body, raw_bytes=True)

    def conf(self, pid, gw):
        return self._req("GET", f"/eliot/plants/{pid}/gateway/{gw}/conf", raw_bytes=True)


def _get(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d:
                return d[k]
    return default


def make_csr(common_name):
    import warnings
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cn = x509.NameAttribute(NameOID.COMMON_NAME, common_name, _validate=False)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, CSR_SUBJECT["C"]),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, CSR_SUBJECT["ST"]),
        x509.NameAttribute(NameOID.LOCALITY_NAME, CSR_SUBJECT["L"]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CSR_SUBJECT["O"]),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, CSR_SUBJECT["OU"]),
        cn,
    ])
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(key, hashes.SHA256())
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()).decode()
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, csr_pem.replace("\n", " ")


def unzip_bytes(blob, password=None):
    if blob[:2] != b"PK":
        try:
            obj = json.loads(blob.decode("utf-8", "replace"))
            if isinstance(obj, dict) and isinstance(obj.get("payload"), str):
                blob = base64.b64decode(obj["payload"])
        except Exception:
            try:
                blob = base64.b64decode(blob)
            except Exception:
                pass
    if blob[:2] != b"PK":
        raise PortalError(f"non è uno ZIP: {blob[:40]!r}")
    files = {}
    if password is None:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                files[os.path.basename(n)] = z.read(n)
        return files
    # cifrato: prova zipfile (ZipCrypto) poi pyzipper (AES)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                files[os.path.basename(n)] = z.read(n, pwd=password)
        return files
    except (RuntimeError, NotImplementedError):
        import pyzipper
        files = {}
        with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
            z.setpassword(password)
            for n in z.namelist():
                files[os.path.basename(n)] = z.read(n)
        return files


def parse_devaddr(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for comm in root.iter("communication"):
        pd = comm.find("p_default")
        if pd is not None:
            dev = pd.findtext("dev")
            addr = pd.findtext("address")
            if dev is not None and addr is not None:
                return f"{dev}{addr}"
    return ""


def parse_locks_from_conf(xml_bytes):
    """Ricava le serrature da conf.xml:
      - serratura principale = vdes/communication/p_default (PE = dev+address)
      - serrature aggiuntive = keyobjects/ko* che hanno <sysaddress> (PE = dev_p_default + sysaddress)
    Su questo impianto dà 20 (p_default) e 21 (ko1 sysaddress=1), verificati sul campo."""
    locks = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return locks
    base_dev = None
    for comm in root.iter("communication"):
        pd = comm.find("p_default")
        if pd is not None:
            dev = (pd.findtext("dev") or "").strip()
            addr = (pd.findtext("address") or "").strip()
            if dev and addr != "":
                base_dev = dev
                locks.append({"name": "Serratura 1", "pe": f"{dev}{addr}"})
        break
    if base_dev:
        for ko_container in root.iter("keyobjects"):
            for child in list(ko_container):
                if child.tag.lower().startswith("ko"):
                    sa = (child.findtext("sysaddress") or "").strip()
                    if sa:
                        pe = f"{base_dev}{sa}"
                        if pe not in [l["pe"] for l in locks]:
                            locks.append({"name": f"Serratura {len(locks) + 1}", "pe": pe})
    return locks


# CID dei dispositivi (da J2/a.java): 10060=serratura(Lock)
CID_LOCK = "10060"


def parse_locks(xml_bytes):
    """Estrae le serrature da archive.xml: ogni elemento con attributo cid=10060 è una
    serratura; l'indirizzo PE = dev + address (come device.g()+device.d() nell'app)."""
    locks = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return locks
    n = 0
    for el in root.iter():
        a = el.attrib
        if a.get("cid") == CID_LOCK:
            n += 1
            dev = a.get("dev", "")
            addr = a.get("address", "")
            pe = f"{dev}{addr}"
            # nome: usa descr/name se presenti e non vuoti, altrimenti "Serratura N"
            raw = (a.get("descr") or a.get("name") or "").strip()
            name = raw if raw else f"Serratura {n}"
            if pe:
                locks.append({"name": name, "pe": pe})
    # se più serrature condividono lo stesso nome (descr vuoti/uguali), numerale tutte
    from collections import Counter
    totals = Counter(lk["name"] for lk in locks)
    idx = {}
    for lk in locks:
        base = lk["name"]
        if totals[base] > 1:
            idx[base] = idx.get(base, 0) + 1
            lk["name"] = f"{base} {idx[base]}"
    return locks


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def add_mod_lines():
    want = ["ctrl_tcp.so", "menu.so", "srtp.so",
            # codec audio: più ne carichiamo, più è probabile trovarne uno in comune col citofono
            # (altrimenti baresip RIFIUTA la chiamata in arrivo, facendola cadere su tutti i device)
            # il citofono offre SPEEX (payload 96) e G.729 (payload 18): servono questi!
            "speex.so", "g729.so",
            "g711.so", "g722.so", "g726.so", "opus.so", "amr.so", "gsm.so",
            "aubridge.so",
            "owncmd.so", "recorder.so",
            "avcodec.so", "swscale.so", "snapshot.so", "fakevideo.so", "account.so"]
    lines = []
    present_audio = []
    audio_mods = {"speex.so", "g729.so", "g711.so", "g722.so", "g726.so",
                  "opus.so", "amr.so", "gsm.so", "l16.so"}
    for m in want:
        if os.path.exists(os.path.join(MODDIR, m)):
            lines.append(f"module                  {m}")
            if m in audio_mods:
                present_audio.append(m)
        else:
            log(f"modulo assente, lo salto: {m}")
    # elenca TUTTI i moduli audio-codec realmente disponibili, per diagnosi
    all_audio = sorted(os.path.basename(x) for x in glob.glob(os.path.join(MODDIR, "*.so"))
                       if os.path.basename(x) in audio_mods
                       or os.path.basename(x) in {"aac.so", "mpa.so", "g7221.so"})
    log(f"codec audio caricati: {present_audio}")
    log(f"codec audio disponibili nel pacchetto: {all_audio}")
    return "\n".join(lines)


def write_baresip_files(sip_account, sip_password, gw_id, ca_path, client_pem):
    os.makedirs(BARESIP_DIR, exist_ok=True)
    domain = f"{gw_id}{SIP_SUFFIX}"
    config = f"""module_path             {MODDIR}
sip_cafile              {ca_path}
sip_certificate         {client_pem}
sip_verify_server       no
# usa il resolver DNS di sistema: serve a risolvere gli host nei Record-Route per instradare
# l'ACK del 200 OK (altrimenti "sa_set_port: no af 0" -> ENOSYS e chiusura della chiamata)
dns_getaddrinfo         yes
# offri solo i codec che il posto esterno usa davvero (riduce i rifiuti 488)
audio_codecs            g729,telephone-event
video_codecs            h264
{add_mod_lines()}
audio_player            recplay,nil
audio_source            aubridge,nil
video_source            fakevideo,nil
video_display           fakevideo,nil
ctrl_tcp_listen         127.0.0.1:4444
# niente tono di chiamata: senza un vero dispositivo audio il ringback va in errore (ENOSYS)
# e abbatteva la chiamata on-demand prima dello snapshot
ringback_disabled       yes
"""
    with open(os.path.join(BARESIP_DIR, "config"), "w") as f:
        f.write(config)
    account = (f"<sip:{sip_account}>;auth_pass={sip_password}"
               f';outbound="sip:{SIP_SERVER};transport=tls"'
               f";answermode=manual;mediaenc=srtp-mand;video=yes;regint=600\n")
    with open(os.path.join(BARESIP_DIR, "accounts"), "w") as f:
        f.write(account)


def main():
    if not EMAIL or not PASSWORD:
        log("ERRORE: imposta email e password dell'account Door Entry nelle opzioni dell'add-on.")
        sys.exit(2)
    os.makedirs(CERT_DIR, exist_ok=True)

    p = Portal()
    log("login…")
    p.sign_in(EMAIL, PASSWORD)
    log("login ok")

    plants = p.plants()
    pl = plants[0]
    pid = _get(pl, "PlantId", "plantId", "Id")
    plant_name = _get(pl, "PlantName", "plantName", default="")
    gws = []
    try:
        gws = p.gateways(pid)
    except PortalError:
        pass
    gw = next((_get(g, "GatewayId", "gatewayId") for g in gws if _get(g, "GatewayId", "gatewayId")),
              _get(pl, "GatewayId"))
    if not gw:
        log("ERRORE: GatewayId non trovato"); sys.exit(2)
    log(f"impianto {pid}, gateway {gw}")

    # stato persistente: IdDevice + SipAccount dell'interno dedicato
    state = load_json(STATE_FILE)
    id_device = state.get("id_device")
    sip_account = state.get("sip_account")
    if not id_device or not sip_account:
        id_device = str(uuid.uuid4()).upper()
        sip_account = f"{EMAIL.replace('@', '-')}-{id_device}@{gw}{SIP_SUFFIX}"
        log(f"creo nuovo interno dedicato: {DEVICE_NAME} (IdDevice {id_device})")
        try:
            p.create_sip_user(sip_account, DEVICE_NAME, gw, id_device)
        except PortalError as e:
            log(f"ERRORE creazione interno: {e}"); sys.exit(2)
        save_json(STATE_FILE, {"id_device": id_device, "sip_account": sip_account})
    else:
        log(f"riuso interno dedicato esistente (IdDevice {id_device})")

    # recupera la password dell'interno; se non c'è, ricrea l'interno con lo stesso IdDevice
    sips = p.sip_users(pid, gw)
    mine = next((s for s in sips if _get(s, "IdDevice", "idDevice") == id_device), None)
    if not mine:
        log("l'interno non risulta più sul server: lo ricreo")
        try:
            p.create_sip_user(sip_account, DEVICE_NAME, gw, id_device)
        except PortalError as e:
            log(f"ERRORE ricreazione interno: {e}"); sys.exit(2)
        sips = p.sip_users(pid, gw)
        mine = next((s for s in sips if _get(s, "IdDevice", "idDevice") == id_device), None)
    if not mine:
        log("ERRORE: impossibile ottenere l'interno creato"); sys.exit(2)
    sip_password = _get(mine, "SipPassword", "sipPassword")
    sip_account = _get(mine, "SipAccount", "sipAccount") or sip_account
    log(f"interno pronto: {sip_account}")

    # certificati: riusa se presenti, altrimenti firma
    cert_path = os.path.join(CERT_DIR, "client.cert.pem")
    key_path = os.path.join(CERT_DIR, "client.key.pem")
    ca_path = os.path.join(CERT_DIR, "ca-chain.cert.pem")
    client_pem = os.path.join(CERT_DIR, "client.pem")
    if not (os.path.exists(cert_path) and os.path.exists(key_path) and os.path.exists(ca_path)):
        log("genero CSR e richiedo il certificato…")
        key_pem, csr_oneline = make_csr(sip_account)
        with open(key_path, "w") as f:
            f.write(key_pem)
        blob = p.signcert(sip_account, csr_oneline)
        files = unzip_bytes(blob)  # archivio cert non cifrato
        cert_file = next((v for k, v in files.items()
                          if k.endswith(".cert.pem") and "ca-chain" not in k), None)
        ca_file = next((v for k, v in files.items() if "ca-chain" in k), None)
        if not cert_file:
            log(f"ERRORE: certificato non trovato nell'archivio ({list(files)})"); sys.exit(2)
        with open(cert_path, "wb") as f:
            f.write(cert_file)
        with open(ca_path, "wb") as f:
            f.write(ca_file if ca_file else cert_file)
        log("certificato ottenuto")
    else:
        log("certificati già presenti, li riuso")
    # file combinato cert+chiave per baresip
    with open(client_pem, "w") as f:
        f.write(open(cert_path).read())
        f.write(open(key_path).read())

    # difesa in profondità: la cartella e i file con la chiave privata sono leggibili solo da root
    try:
        os.chmod(CERT_DIR, 0o700)
        for pth in (key_path, client_pem):
            if os.path.exists(pth):
                os.chmod(pth, 0o600)
        for pth in (cert_path, ca_path):
            if os.path.exists(pth):
                os.chmod(pth, 0o644)
    except OSError as e:
        log(f"impossibile impostare i permessi dei certificati (non bloccante): {e}")

    # DEVADDR + discovery serrature dalla configurazione; riprova il download (può fallire per
    # rate-limit/transienti) e tiene separate le serrature scoperte, così un successo "resta".
    devaddr = state.get("devaddr", "")
    found = []
    conf_files = {}
    for attempt in range(3):
        try:
            conf_files = unzip_bytes(p.conf(pid, gw), password=ZIP_PASSWORD)
            break
        except Exception as e:
            log(f"download configurazione fallito (tentativo {attempt + 1}/3): {e}")
            time.sleep(3)
    if conf_files:
        if not devaddr and "conf.xml" in conf_files:
            devaddr = parse_devaddr(conf_files["conf.xml"])
            log(f"DEVADDR ricavato: {devaddr}")
        if "conf.xml" in conf_files:
            found = parse_locks_from_conf(conf_files["conf.xml"])
        if not found and "archive.xml" in conf_files:
            found = parse_locks(conf_files["archive.xml"])
        log(f"discovery serrature: {len(found)}" +
            ("" if not found else " -> " + ", ".join(f"{l['name']}(PE {l['pe']})" for l in found)))

    discovered_cache = state.get("discovered_locks", [])  # ultimo discovery riuscito

    # PRIORITÀ: discovery di ora > ultimo discovery riuscito > DEVADDR (serratura singola).
    if found:
        locks = found
        discovered_cache = found
        log(f"serrature: uso il discovery ({len(locks)})")
    elif discovered_cache:
        locks = discovered_cache
        log(f"serrature: riuso l'ultimo discovery riuscito ({len(locks)})")
    elif devaddr:
        locks = [{"name": "Serratura 1", "pe": devaddr}]
        log("serrature: ripiego sul DEVADDR (una sola)")
    else:
        locks = []
        log("serrature: nessuna sorgente disponibile")

    # elenco interni/dispositivi (per i sensori informativi in HA)
    devices = []
    try:
        for s in sips:
            devices.append({
                "name": _get(s, "DeviceName", "deviceName", default="?"),
                "id_device": _get(s, "IdDevice", "idDevice"),
            })
    except Exception:
        pass

    write_baresip_files(sip_account, sip_password, gw, ca_path, client_pem)
    save_json(STATE_FILE, {"id_device": id_device, "sip_account": sip_account,
                           "devaddr": devaddr, "discovered_locks": discovered_cache})
    save_json(RUNTIME_FILE, {
        "sip_account": sip_account,
        "gw_domain": f"{gw}{SIP_SUFFIX}",
        "devaddr": devaddr,
        "locks": locks,
        "plant_name": plant_name,
        "plant_id": pid,
        "gateway_id": gw,
        "device_count": len(devices),
        "devices": devices,
    })
    log("provisioning completato")


if __name__ == "__main__":
    main()
