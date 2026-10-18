#!/usr/bin/env python3
"""
c300x_devaddr.py — scopre il DEVADDR (indirizzo del posto esterno) del tuo impianto Classe 300X,
replicando ciò che fa l'app Door Entry.

Dal reverse engineering (j0/C0938n.f + l2/VctGetPlantConfigController + K2/MhpXmlConfParser):
  1) GET /eliot/plants/{plantId}/gateway/{gatewayId}/conf   -> archivio ZIP con conf.xml
  2) in conf.xml, sotto /configuratore/setup/vdes/communication/ :
        p_default/dev      -> parte "dev"      (MhpDevice.g())
        p_default/address  -> parte "address"  (MhpDevice.d())
     DEVADDR del posto esterno di default = dev + address
Le telecamere (TVCC) hanno CID 10061; lo script elenca anche eventuali altri indirizzi trovati.

USO:
  python3 c300x_devaddr.py <email> <password>
  python3 c300x_devaddr.py <email> <password> --save-conf ./conf   (salva i file estratti)
  python3 c300x_devaddr.py <email> <password> --plant <PlantId>    (se hai più impianti)
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile

PRJ_NAME = "C3X"
APP_VERSION = "legacy-1.17.5"
PORTAL = "https://www.myhomeweb.com"


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self, base=PORTAL, verbose=False):
        self.base = base.rstrip("/"); self.token = None; self.verbose = verbose

    def _req(self, method, path, *, body=None, raw_bytes=False):
        url = self.base + path
        h = {"prj_name": PRJ_NAME}
        if self.token:
            h["auth_token"] = self.token
        if body is not None:
            h["Content-Type"] = "application/json"
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
                return blob if raw_bytes else blob.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise PortalError(f"{method} {path} -> HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
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
            raise PortalError("login ok ma nessun auth_token")
        return self.payload(raw)

    def plants(self):
        d = self.payload(self._req("GET", "/eliot/plants"))
        return d if isinstance(d, list) else [d]

    def gateways(self, plant_id):
        d = self.payload(self._req("GET", f"/eliot/plants/{plant_id}/gateway/"))
        return d if isinstance(d, list) else [d]

    def conf(self, plant_id, gateway_id):
        # GET /eliot/plants/{plant}/gateway/{gw}/conf  -> ZIP binario
        return self._req("GET", f"/eliot/plants/{plant_id}/gateway/{gateway_id}/conf",
                         raw_bytes=True)


def _get(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if k in d:
                return d[k]
    return default


def unzip(blob):
    if blob[:2] != b"PK":
        # prova base64 / json payload
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
        raise PortalError(f"risposta /conf non è uno ZIP. Primi byte: {blob[:60]!r}")
    files = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            files[os.path.basename(name)] = z.read(name)
    return files


def parse_conf_xml(xml_bytes):
    """Estrae DEVADDR dal conf.xml. Ritorna dict con i valori trovati."""
    out = {"p_default_dev": None, "p_default_address": None,
           "communication_address": None, "devaddr": None, "all_blocks": []}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise PortalError(f"conf.xml non valido: {e}")

    # i percorsi dell'app: /configuratore/setup/vdes/communication/...
    for comm in root.iter("communication"):
        block = {}
        pd = comm.find("p_default")
        if pd is not None:
            dev = pd.findtext("dev")
            addr = pd.findtext("address")
            block["p_default_dev"] = dev
            block["p_default_address"] = addr
            if out["p_default_dev"] is None:
                out["p_default_dev"] = dev
                out["p_default_address"] = addr
        ca = comm.findtext("address")
        if ca is not None:
            block["communication_address"] = ca
            if out["communication_address"] is None:
                out["communication_address"] = ca
        if block:
            out["all_blocks"].append(block)

    if out["p_default_dev"] is not None and out["p_default_address"] is not None:
        out["devaddr"] = f"{out['p_default_dev']}{out['p_default_address']}"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--base", default=PORTAL)
    ap.add_argument("--plant", help="PlantId specifico")
    ap.add_argument("--save-conf", help="cartella dove salvare i file estratti (conf.xml, ...)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    p = Portal(base=args.base, verbose=args.verbose)
    print("· Login…"); p.sign_in(args.username, args.password); print("  ok.")

    for pl in p.plants():
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
            print(f"\n· Impianto '{pname}' / gateway {gwid}: scarico la configurazione…")
            try:
                blob = p.conf(pid, gwid)
            except PortalError as e:
                print(f"  errore scaricando /conf: {e}"); continue
            try:
                files = unzip(blob)
            except PortalError as e:
                print(f"  {e}"); continue
            print(f"  archivio ricevuto, file: {', '.join(sorted(files))}")

            if args.save_conf:
                os.makedirs(args.save_conf, exist_ok=True)
                for name, data in files.items():
                    with open(os.path.join(args.save_conf, name), "wb") as f:
                        f.write(data)
                print(f"  file salvati in {args.save_conf}")

            if "conf.xml" not in files:
                print("  conf.xml non presente nell'archivio."); continue
            info = parse_conf_xml(files["conf.xml"])
            print("\n  === RISULTATO ===")
            print(f"  p_default/dev     = {info['p_default_dev']}")
            print(f"  p_default/address = {info['p_default_address']}")
            print(f"  communication/address = {info['communication_address']}")
            if info["devaddr"]:
                print(f"\n  >>> DEVADDR (posto esterno di default) = {info['devaddr']}")
                print("      Usa questo valore per la cattura on-demand.")
            else:
                print("\n  DEVADDR non ricavato automaticamente. Apri conf.xml con --save-conf e")
                print("  cerca a mano il blocco <vdes><communication>, dimmi cosa contiene.")
            if len(info["all_blocks"]) > 1:
                print(f"  (trovati {len(info['all_blocks'])} blocchi communication: {info['all_blocks']})")


if __name__ == "__main__":
    main()
