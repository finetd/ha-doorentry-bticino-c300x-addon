#!/usr/bin/env python3
"""
doorbell.py — supervisore di baresip per il videocitofono Classe 300X in Home Assistant.

Funzioni:
  - SQUILLO: quando suonano, lancia l'evento HA per la notifica (notify_only, sicuro);
    in fallback_snapshot, dopo un timeout senza risposta, accetta e cattura uno screenshot.
  - CONTROLLI: un piccolo server HTTP permette a Home Assistant (via rest_command / rest sensor):
      GET /info                 -> JSON con info impianto (per sensori)
      GET /cmd/answering_on     -> segreteria ON   (*8*92##)
      GET /cmd/answering_off    -> segreteria OFF  (*8*91##)
      GET /cmd/light            -> luce scale      (*13*35*##)
      GET /cmd/door             -> apri porta      (*8*19*<PE>## + *8*20*<PE>##)
    I comandi sono inviati al citofono come SIP MESSAGE (frame OpenWebNet) dal modulo 'owncmd'.
"""

import glob
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "/share/doorentry_bticino_c300x/doorbell.jpg")
HA_EVENT = os.environ.get("HA_EVENT", "doorentry_bticino_c300x_ring")
SNAPSHOT_DELAY = float(os.environ.get("SNAPSHOT_DELAY", "2.0"))
# quanti tentativi di snapshot (~1s l'uno): finestra ampia per l'autoaccensione, che può
# impiegare diversi secondi prima di trasmettere il video
CAPTURE_ATTEMPTS = int(os.environ.get("CAPTURE_ATTEMPTS", "25"))
WORKDIR = os.environ.get("WORKDIR", "/share/doorentry_bticino_c300x")
CAPTURE_MODE = os.environ.get("CAPTURE_MODE", "notify_only")
FALLBACK_DELAY = float(os.environ.get("FALLBACK_DELAY", "20"))
# per quanto tenere "acceso" lo stato squillo, così l'integrazione (poll 2s) lo cattura sempre
RING_HOLD = max(15.0, FALLBACK_DELAY + 10.0)
CTRL_PORT_HTTP = int(os.environ.get("HTTP_PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
WEBHOOK_ID = os.environ.get("WEBHOOK_ID", "").strip()
HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://homeassistant:8123").rstrip("/")
CTRL_HOST, CTRL_PORT = "127.0.0.1", 4444


def _runtime():
    try:
        with open("/data/runtime.json") as f:
            return json.load(f)
    except Exception:
        return {}

RT = _runtime()
DEVADDR = RT.get("devaddr", "") or "20"
LOCKS = RT.get("locks") or [{"name": "Serratura", "pe": DEVADDR}]


def _door_frames(pe):
    return [f"*8*19*{pe}##", f"*8*20*{pe}##"]


# frame OpenWebNet per i comandi fissi
FRAMES = {
    "answering_on":  ["*8*92##"],
    "answering_off": ["*8*91##"],
    "light":         ["*13*35*##"],
    "door":          _door_frames(LOCKS[0]["pe"]),   # 'door' = prima serratura (compatibilità)
}

_ctrl_sock = None
_ctrl_lock = threading.Lock()

# cattura on-demand SPERIMENTALE: attiva una chiamata uscente verso il posto esterno
OUTGOING_CAPTURE = {"active": False}
# registrazione on-demand: come la cattura, ma registra audio+video per N secondi
OUTGOING_RECORD = {"active": False, "duration": 30, "basepath": "", "mp4": ""}

RECORDINGS_DIR = os.path.join(WORKDIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ripulisce le vecchie registrazioni con timestamp (ora teniamo solo last.mp4)
for _f in os.listdir(RECORDINGS_DIR):
    if _f != "last.mp4" and _f.endswith(".mp4"):
        try: os.remove(os.path.join(RECORDINGS_DIR, _f))
        except OSError: pass

# stato condiviso letto dall'integrazione HA via /state
STATE = {"ringing": False, "ring_count": 0, "last_ring": 0.0,
         "missed": False, "missed_count": 0, "last_missed": 0.0,
         "registered": False, "last_register": 0.0}
# se non arriva un REGISTER_OK entro questo tempo, consideriamo la connessione persa.
# La registrazione si rinnova ~ogni 9 min (regint 600s): diamo margine.
REGISTER_TIMEOUT = 800.0
_ring_timer = None
_missed_timer = None
MISSED_HOLD = 30.0   # per quanto tenere "acceso" lo stato Chiamata persa (s)


def log(*a):
    print(time.strftime("%H:%M:%S"), "[doorentry-bticino-c300x]", *a, flush=True)


def _mark_ringing():
    """Segna lo stato 'sta suonando' e lo azzera dopo 20s (così l'integrazione non lo perde)."""
    global _ring_timer
    STATE["ringing"] = True
    STATE["ring_count"] += 1
    STATE["last_ring"] = time.time()
    if _ring_timer:
        _ring_timer.cancel()

    def _clear():
        STATE["ringing"] = False
    _ring_timer = threading.Timer(RING_HOLD, _clear)
    _ring_timer.daemon = True
    _ring_timer.start()


def _mark_missed():
    """Segna 'chiamata persa' (acceso brevemente) quando scatta la cattura di fallback."""
    global _missed_timer
    STATE["missed"] = True
    STATE["missed_count"] += 1
    STATE["last_missed"] = time.time()
    if _missed_timer:
        _missed_timer.cancel()

    def _clear():
        STATE["missed"] = False
    _missed_timer = threading.Timer(MISSED_HOLD, _clear)
    _missed_timer.daemon = True
    _missed_timer.start()


def fire_ha_event(extra=None):
    body = {"snapshot": SNAPSHOT_PATH, "ts": time.time()}
    if extra:
        body.update(extra)
    data = json.dumps(body).encode()

    # metodo 1 (consigliato, senza token): webhook di Home Assistant
    if WEBHOOK_ID:
        url = f"{HA_BASE_URL}/api/webhook/{WEBHOOK_ID}"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"webhook '{WEBHOOK_ID}' inviato (HTTP {r.status})")
            return
        except Exception as e:
            log(f"invio webhook fallito ({url}): {e}")
            # prova comunque l'evento col token, se c'è
    # metodo 2: evento via API Supervisor (richiede il token)
    if SUPERVISOR_TOKEN:
        url = f"http://supervisor/core/api/events/{HA_EVENT}"
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"evento HA '{HA_EVENT}' inviato (HTTP {r.status})")
            return
        except Exception as e:
            log(f"invio evento HA fallito: {e}")
            return
    log("nessun metodo di notifica disponibile: imposta 'webhook_id' nelle opzioni dell'add-on")


def send_cmd(command, params=None):
    """Invia un comando a baresip via ctrl_tcp (thread-safe)."""
    global _ctrl_sock
    obj = {"command": command}
    if params is not None:
        obj["params"] = params
    payload = json.dumps(obj)
    data = f"{len(payload)}:{payload},".encode()
    with _ctrl_lock:
        if _ctrl_sock is None:
            return False
        try:
            _ctrl_sock.sendall(data)
            return True
        except OSError as e:
            log(f"invio ctrl fallito: {e}")
            return False


def send_frames(frames):
    ok = True
    for fr in frames:
        if not send_cmd("owncmd", fr):
            ok = False
        time.sleep(0.4)
    return ok


def _newest_png_after(ts):
    # baresip salva 'snapshot-send-*' (il NOSTRO video, barre RGB di fakevideo) e
    # 'snapshot-recv-*' (il video RICEVUTO dal citofono): a noi serve solo quest'ultimo.
    pats = ["snapshot-recv-*.png"]
    cand = []
    for base in (WORKDIR, os.path.expanduser("~")):
        for pat in pats:
            cand += glob.glob(os.path.join(base, pat))
    cand = [p for p in cand if os.path.getmtime(p) > ts]
    return max(cand, key=os.path.getmtime) if cand else None


def _cleanup_pngs():
    """Rimuove tutti i PNG temporanei creati dal modulo snapshot (send e recv)."""
    for base in (WORKDIR, os.path.expanduser("~")):
        for p in glob.glob(os.path.join(base, "snapshot-*.png")):
            try:
                os.remove(p)
            except OSError:
                pass


def _run_outgoing_record():
    """Esegue la registrazione: start, attesa, stop, muxing MP4 con ffmpeg, hangup."""
    import subprocess
    base = OUTGOING_RECORD["basepath"]
    mp4 = OUTGOING_RECORD["mp4"]
    dur = OUTGOING_RECORD["duration"]
    # dai qualche secondo al video per iniziare (keyframe + camera warm-up), come /cmd/capture
    time.sleep(min(SNAPSHOT_DELAY, 2.0))
    for _ in range(8):           # alcune FIR per far partire il video
        send_cmd("keyframe")
        time.sleep(0.5)
    send_cmd("recorder_start", base)
    log(f"registrazione in corso per {dur}s...")
    time.sleep(dur)
    send_cmd("recorder_stop")
    time.sleep(0.3)
    send_cmd("hangup")
    OUTGOING_RECORD["active"] = False

    wav, y4m = f"{base}.wav", f"{base}.y4m"
    if not (os.path.exists(wav) and os.path.exists(y4m)):
        log("registrazione: file mancanti, muxing annullato"); return
    log(f"file intermedi: wav={os.path.getsize(wav)} byte, y4m={os.path.getsize(y4m)} byte")

    # fps reale = frame scritti / durata di registrazione effettiva.
    # NB: calcoliamo sulla durata della finestra (dur secondi), non su quella dell'audio,
    # perché l'audio potrebbe non esserci (bug noto). Il video arriva a ~9-10 fps dal C300X.
    try:
        with open(y4m, "rb") as f:
            nframes = f.read().count(b"FRAME\n")
        audio_bytes = max(0, os.path.getsize(wav) - 44)
        audio_dur = audio_bytes / (8000 * 2)
        # usiamo dur (la finestra di registrazione) come riferimento di durata
        fps_real = max(1.0, min(30.0, nframes / dur)) if nframes > 1 else 10.0
        log(f"muxing MP4: {nframes} frame video / {dur}s finestra -> {fps_real:.2f} fps "
            f"(audio={audio_bytes} byte = {audio_dur:.1f}s)")
    except Exception as e:
        log(f"calcolo fps fallito, uso 10 fps: {e}"); fps_real = 10.0

    # muxing robusto: se un flusso manca (video non arrivato dal citofono, o audio zero),
    # salviamo comunque in MP4 quello che c'è, senza dare errore a ffmpeg
    y4m_ok = os.path.getsize(y4m) > 100   # almeno l'header Y4M (~40 byte) + 1 frame
    wav_ok = audio_bytes > 1000           # almeno ~0.03s di audio
    log(f"muxing MP4 con ffmpeg: {mp4} (video_ok={y4m_ok}, audio_ok={wav_ok})")
    try:
        if y4m_ok and wav_ok:
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-r", f"{fps_real:.3f}", "-i", y4m, "-i", wav,
                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "64k", mp4]
        elif y4m_ok:
            log("audio mancante o troppo corto, salvo MP4 solo video")
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-r", f"{fps_real:.3f}", "-i", y4m,
                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", mp4]
        elif wav_ok:
            log("video mancante (il citofono non ha mandato), salvo MP4 solo audio")
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-i", wav, "-c:a", "aac", "-b:a", "64k", mp4]
        else:
            log("niente audio ne' video, muxing annullato"); return
        subprocess.run(cmd, check=True, timeout=120)
        log(f"registrazione salvata: {mp4}")
    except Exception as e:
        log(f"ffmpeg fallito: {e}")
    finally:
        for f in (wav, y4m):
            try: os.remove(f)
            except OSError: pass


def capture_snapshot():
    """Scatta lo snapshot in modo robusto: all'inizio dello stream H.264 i primi pacchetti
    non hanno ancora un keyframe (errori 'non-existing PPS'), quindi riproviamo finché baresip
    non produce un PNG nuovo (= fotogramma valido decodificato). I PNG temporanei vengono poi
    cancellati: su disco resta solo doorbell.jpg (sovrascritto ogni volta)."""
    t0 = time.time()
    ok = False
    try:
        # In autoaccensione il posto esterno accende la telecamera e inizia a trasmettere con
        # qualche secondo di ritardo (l'audio stesso parte dopo ~5s): diamo una finestra ampia.
        # capture_snapshot esce comunque appena arriva il primo fotogramma valido.
        for attempt in range(CAPTURE_ATTEMPTS):
            # sollecita il video: il posto esterno in autoaccensione invia il flusso solo dopo
            # una richiesta di keyframe (RTCP FIR). La chiediamo ripetutamente nei primi secondi.
            if attempt < 10:
                send_cmd("keyframe")
            send_cmd("snapshot")
            time.sleep(1.0)
            png = _newest_png_after(t0)
            if png:
                try:
                    subprocess.run(["ffmpeg", "-y", "-i", png, SNAPSHOT_PATH],
                                   check=True, capture_output=True)
                    log(f"screenshot salvato in {SNAPSHOT_PATH} (tentativo {attempt + 1})")
                    ok = True
                except Exception as e:
                    log(f"conversione ffmpeg fallita: {e}")
                break
        if not ok:
            log("nessun fotogramma valido ricevuto (keyframe non arrivato in tempo)")
    finally:
        _cleanup_pngs()   # non lasciare PNG sul disco
    return ok


def read_netstrings(buf):
    out = []
    while True:
        if b":" not in buf:
            break
        length_str, rest = buf.split(b":", 1)
        if not length_str.isdigit():
            buf = rest
            continue
        n = int(length_str)
        if len(rest) < n + 1:
            break
        out.append(rest[:n])
        buf = rest[n + 1:]
    return out, buf


def _status_page():
    """Pagina HTML di stato, mostrata aprendo la porta 8099 (pulsante 'Visita' dell'add-on)."""
    import html

    def esc(x):
        return html.escape(str(x if x is not None else "—"))

    ringing = STATE["ringing"]
    missed = STATE["missed"]

    def ago(ts):
        if not ts:
            return "mai"
        d = int(time.time() - ts)
        if d < 60:
            return f"{d}s fa"
        if d < 3600:
            return f"{d // 60}m fa"
        return time.strftime("%d/%m %H:%M", time.localtime(ts))

    locks_rows = "".join(
        f"<tr><td>{esc(l.get('name'))}</td><td><code>{esc(l.get('pe'))}</code></td>"
        f"<td><a class='btn' href='/cmd/lock/{esc(l.get('pe'))}' target='_blank'>Apri</a></td></tr>"
        for l in LOCKS
    ) or "<tr><td colspan='3'>nessuna</td></tr>"

    dev_rows = "".join(
        f"<tr><td>{esc(d.get('name'))}</td><td><code>{esc(d.get('id_device'))}</code></td></tr>"
        for d in (RT.get("devices") or [])
    ) or "<tr><td colspan='2'>—</td></tr>"

    snap_ts = 0.0
    try:
        snap_ts = os.path.getmtime(SNAPSHOT_PATH)
    except OSError:
        pass

    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Door Entry BTicino C300X</title>
<style>
 :root{{color-scheme:light dark}}
 body{{font-family:system-ui,sans-serif;margin:0;padding:1.2rem;max-width:760px;margin:auto;
   background:#0e1116;color:#e6e6e6}}
 @media (prefers-color-scheme:light){{body{{background:#f5f5f7;color:#111}}}}
 h1{{font-size:1.3rem;display:flex;align-items:center;gap:.5rem}}
 .card{{background:#1b2130;border-radius:12px;padding:1rem 1.2rem;margin:.8rem 0}}
 @media (prefers-color-scheme:light){{.card{{background:#fff;box-shadow:0 1px 4px #0001}}}}
 .pill{{display:inline-block;padding:.15rem .6rem;border-radius:999px;font-size:.85rem;font-weight:600}}
 .on{{background:#1e7e34;color:#fff}} .off{{background:#444;color:#ccc}}
 table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:.35rem .4rem;border-bottom:1px solid #ffffff14}}
 code{{background:#ffffff14;padding:.1rem .35rem;border-radius:5px}}
 a.btn{{background:#2b6cb0;color:#fff;text-decoration:none;padding:.25rem .7rem;border-radius:7px;font-size:.85rem}}
 .muted{{opacity:.7;font-size:.9rem}} .row{{display:flex;gap:1.5rem;flex-wrap:wrap}}
 img{{max-width:100%;border-radius:10px;margin-top:.5rem}}
</style></head><body>
<h1>🔔 Door Entry BTicino C300X</h1>

<div class="card">
  <div class="row">
    <div>Sta suonando: <span class="pill {'on' if ringing else 'off'}">{'SÌ' if ringing else 'no'}</span></div>
    <div>Chiamata persa: <span class="pill {'on' if missed else 'off'}">{'SÌ' if missed else 'no'}</span></div>
  </div>
  <p class="muted">Squilli totali: {STATE['ring_count']} (ultimo {ago(STATE['last_ring'])}) ·
     Perse: {STATE['missed_count']} (ultima {ago(STATE['last_missed'])}) ·
     Modalità: <code>{esc(CAPTURE_MODE)}</code></p>
</div>

<div class="card">
  <strong>Impianto</strong>
  <p class="muted">{esc(RT.get('plant_name'))} · Gateway <code>{esc(RT.get('gateway_id'))}</code> ·
     DEVADDR <code>{esc(RT.get('devaddr'))}</code> · Interni: {esc(RT.get('device_count'))}</p>
</div>

<div class="card">
  <strong>Serrature</strong>
  <table><tr><th>Nome</th><th>PE</th><th></th></tr>{locks_rows}</table>
  <p class="muted">Test manuale: <code>/cmd/raw?f=*8*19*21##</code></p>
</div>

<div class="card">
  <strong>Comandi</strong><br>
  <a class="btn" href="/cmd/light" target="_blank">Luce scale</a>
  <a class="btn" href="/cmd/answering_on" target="_blank">Segreteria ON</a>
  <a class="btn" href="/cmd/answering_off" target="_blank">Segreteria OFF</a>
</div>

<div class="card">
  <strong>Ultima istantanea</strong> <span class="muted">({ago(snap_ts)})</span><br>
  {"<img src='/snapshot?t="+str(int(snap_ts))+"'>" if snap_ts else "<p class='muted'>nessuna istantanea</p>"}
</div>

<p class="muted">Interni: {dev_rows and ''}</p>
<div class="card"><strong>Interni registrati</strong><table><tr><th>Nome</th><th>IdDevice</th></tr>{dev_rows}</table></div>

<p class="muted">Dati grezzi: <a href="/state" target="_blank">/state</a> · Aggiorna la pagina per rinfrescare.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "":
            self._send(200, _status_page().encode(), "text/html; charset=utf-8")
        elif path == "/state":
            snap_mtime = 0.0
            try:
                snap_mtime = os.path.getmtime(SNAPSHOT_PATH)
            except OSError:
                pass
            st = {
                "ringing": STATE["ringing"],
                "ring_count": STATE["ring_count"],
                "last_ring": STATE["last_ring"],
                "connected": STATE["registered"]
                and (time.time() - STATE["last_register"] < REGISTER_TIMEOUT),
                "last_register": STATE["last_register"],
                "missed": STATE["missed"],
                "missed_count": STATE["missed_count"],
                "last_missed": STATE["last_missed"],
                "snapshot_mtime": snap_mtime,
                "capture_mode": CAPTURE_MODE,
                "plant_name": RT.get("plant_name"),
                "plant_id": RT.get("plant_id"),
                "gateway_id": RT.get("gateway_id"),
                "devaddr": RT.get("devaddr"),
                "device_count": RT.get("device_count"),
                "devices": RT.get("devices"),
                "locks": LOCKS,
            }
            self._send(200, json.dumps(st).encode(), "application/json")
        elif path == "/snapshot":
            try:
                with open(SNAPSHOT_PATH, "rb") as f:
                    data = f.read()
                self._send(200, data, "image/jpeg")
            except OSError:
                self._send(404, b"nessuna istantanea disponibile")
        elif path == "/info":
            info = {k: RT.get(k) for k in ("plant_name", "plant_id", "gateway_id",
                                           "devaddr", "device_count", "devices")}
            self._send(200, json.dumps(info).encode(), "application/json")
        elif path == "/debug/conf":
            # Mostra conf.xml e i candidati DEVADDR per diagnostica
            # Cerca in /data/conf/ (dove provision.py salva) oppure /data/
            conf_path = None
            for p in ("/data/conf/conf.xml", "/data/conf.xml"):
                if os.path.exists(p):
                    conf_path = p; break
            if not conf_path:
                try:
                    files = os.listdir("/data/conf")
                except OSError:
                    files = []
                self._send(404,
                    (f"conf.xml non trovato. File in /data/conf/: {files}\n"
                     "Riavvia l'add-on per far rifare il provisioning e salvare la conf.").encode())
                return
            try:
                with open(conf_path) as f:
                    conf = f.read()
            except OSError as e:
                self._send(500, f"errore lettura: {e}".encode()); return
            # Estrai tutti gli attributi significativi
            import re as _re
            candidati = []
            for m in _re.finditer(r'(dev|p_default|address|sysaddress|where)="([^"]+)"', conf):
                candidati.append(f"{m.group(1)}={m.group(2)}")
            body = (f"# File usato: {conf_path}\n"
                    "# Candidati DEVADDR (prova questi valori nell'opzione sdp_devaddr):\n"
                    + "\n".join(sorted(set(candidati)))
                    + "\n\n# conf.xml completo:\n" + conf)
            self._send(200, body.encode(), "text/plain; charset=utf-8")
        elif path == "/cmd/record":
            # registra audio+video per N secondi (default 30) e salva un MP4
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                duration = max(5, min(120, int((q.get("duration") or ["30"])[0])))
            except ValueError:
                duration = 30
            gw = RT.get("gw_domain", "")
            if not gw:
                self._send(500, b"dominio gateway sconosciuto"); return
            ts = time.strftime("%Y%m%d-%H%M%S")
            base = os.path.join("/tmp", f"rec-{ts}")
            mp4 = os.path.join(RECORDINGS_DIR, "last.mp4")   # sempre lo stesso nome: solo l'ultima
            OUTGOING_RECORD.update({"active": True, "duration": duration,
                                    "basepath": base, "mp4": mp4})
            peer = f"sip:c300x@{gw}"
            log(f"registrazione on-demand: {duration}s -> {mp4}")
            # Il citofono manda media solo con video=sendrecv (anche con DEVADDR corretto).
            # video=recvonly riceve niente. L'app ufficiale usa recvonly ma probabilmente
            # ha anche altre differenze non ancora identificate che permettono quella direzione.
            send_cmd("dialdir", f"{peer} audio=sendrecv video=sendrecv")
            self._send(200, (f"registrazione avviata ({duration}s), file: {mp4}").encode())
        elif path == "/cmd/capture":
            # SPERIMENTALE: chiama il posto esterno e cattura (per testare la patch sa_af)
            gw = RT.get("gw_domain", "")
            if not gw:
                self._send(500, b"dominio gateway sconosciuto"); return
            peer = f"sip:c300x@{gw}"
            OUTGOING_CAPTURE["active"] = True
            log(f"cattura on-demand (sperimentale): chiamo {peer}")
            # video=sendrecv: alcuni posti esterni trasmettono il video solo se "vedono" traffico
            # dalla nostra parte (media simmetrico). baresip invia il fakevideo, noi riceviamo il
            # loro flusso e lo catturiamo. audio=sendrecv come fa l'app.
            send_cmd("dialdir", f"{peer} audio=sendrecv video=sendrecv")
            self._send(200, b"chiamata avviata, guarda i log")
        elif path.startswith("/cmd/raw"):
            # test manuale: invia un frame OpenWebNet qualsiasi -> /cmd/raw?f=*8*19*21##
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            frame = (q.get("f") or [""])[0]
            if not frame:
                self._send(400, b"uso: /cmd/raw?f=*8*19*21##"); return
            log(f"comando HTTP raw: {frame}")
            ok = send_frames([frame])
            self._send(200 if ok else 500, (b"ok" if ok else b"errore"))
        elif path.startswith("/cmd/lock/"):
            # apri una serratura specifica per indirizzo PE: /cmd/lock/<pe>
            pe = path[len("/cmd/lock/"):]
            if not pe.isdigit():
                self._send(400, b"PE non valido"); return
            frames = _door_frames(pe)
            log(f"comando HTTP: apri serratura PE {pe} -> {frames}")
            ok = send_frames(frames)
            self._send(200 if ok else 500, (b"ok" if ok else b"errore"))
        elif path.startswith("/cmd/"):
            action = path[len("/cmd/"):]
            frames = FRAMES.get(action)
            if not frames:
                self._send(404, b"comando sconosciuto")
                return
            log(f"comando HTTP: {action} -> {frames}")
            ok = send_frames(frames)
            self._send(200 if ok else 500, (b"ok" if ok else b"errore"))
        else:
            self._send(404, b"not found")

    def log_message(self, *a):
        pass


def http_server():
    try:
        srv = HTTPServer(("0.0.0.0", CTRL_PORT_HTTP), Handler)
        log(f"server comandi/info su http://<ip-ha>:{CTRL_PORT_HTTP}/  (/info, /cmd/...)")
        srv.serve_forever()
    except Exception as e:
        log(f"server HTTP non avviato: {e}")


def control_loop():
    global _ctrl_sock
    sock = None
    for _ in range(60):
        try:
            sock = socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=2)
            break
        except OSError:
            time.sleep(1)
    if not sock:
        log("non riesco a collegarmi a ctrl_tcp di baresip")
        return
    with _ctrl_lock:
        _ctrl_sock = sock
    log("collegato a baresip (ctrl_tcp)")
    sock.settimeout(1.0)

    buf = b""
    state = {"call_active": False, "we_accepted": False, "timer": None}

    def cancel_timer():
        if state["timer"]:
            state["timer"].cancel()
            state["timer"] = None

    def do_accept():
        # chiamato dopo il timeout (fallback_snapshot) se nessuno ha risposto
        if state["call_active"] and not state["we_accepted"]:
            state["we_accepted"] = True
            log("nessuno ha risposto: chiamata persa, accetto per catturare l'immagine")
            _mark_missed()
            send_cmd("accept")

    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                log("ctrl_tcp chiuso"); return
            buf += chunk
        except socket.timeout:
            continue
        except OSError as e:
            log(f"errore ctrl_tcp: {e}"); return

        msgs, buf = read_netstrings(buf)
        for raw in msgs:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            if ev.get("event") and etype:
                log(f"evento baresip: {etype}")

            if etype == "REGISTER_OK":
                STATE["registered"] = True
                STATE["last_register"] = time.time()
            elif etype in ("REGISTER_FAIL", "UNREGISTERING"):
                STATE["registered"] = False

            if etype == "CALL_INCOMING":
                _mark_ringing()
                state["call_active"] = True
                state["we_accepted"] = False
                fire_ha_event()
                if CAPTURE_MODE == "fallback_snapshot":
                    log(f"fallback_snapshot: attendo {FALLBACK_DELAY:.0f}s; se nessuno risponde, catturo")
                    cancel_timer()
                    t = threading.Timer(FALLBACK_DELAY, do_accept)
                    t.daemon = True
                    t.start()
                    state["timer"] = t
                else:
                    log("notify_only: squilla, non rispondo (lascio squillare gli altri)")

            if etype == "CALL_ESTABLISHED" and OUTGOING_RECORD["active"]:
                # IMPORTANTE: eseguiamo in un thread per NON bloccare il ctrl_loop che riceve
                # gli eventi di baresip. Un time.sleep(30) qui farebbe perdere eventi e,
                # peggio, interromperebbe il flusso di comandi mantenendo la chiamata appesa.
                threading.Thread(target=_run_outgoing_record, daemon=True).start()
            elif etype == "CALL_ESTABLISHED" and (state["we_accepted"] or OUTGOING_CAPTURE["active"]):
                def _do_capture():
                    time.sleep(SNAPSHOT_DELAY)
                    capture_snapshot()
                    time.sleep(0.3)
                    send_cmd("hangup")
                    OUTGOING_CAPTURE["active"] = False
                threading.Thread(target=_do_capture, daemon=True).start()

            if etype == "CALL_CLOSED":
                # se la chiamata si chiude prima del timeout (qualcuno ha risposto altrove),
                # annulliamo il timer di cattura: niente foto, nessuna interferenza.
                # NON azzeriamo subito "ringing": lo lascia spegnere al timer RING_HOLD, così
                # l'integrazione (che interroga ogni 2s) riesce sempre a rilevare lo squillo.
                cancel_timer()
                state["call_active"] = False
                state["we_accepted"] = False


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    os.chdir(WORKDIR)
    _cleanup_pngs()   # rimuovi eventuali PNG residui da sessioni precedenti
    # diagnostica: nomi (non valori) delle variabili d'ambiente, per capire se c'è il token
    log("variabili d'ambiente presenti: " + ", ".join(sorted(os.environ.keys())))
    log(f"token rilevato: {'sì' if SUPERVISOR_TOKEN else 'NO'}")
    threading.Thread(target=http_server, daemon=True).start()
    log("avvio baresip…")
    env = dict(os.environ)
    gw_domain = RT.get("gw_domain", "")
    if gw_domain:
        env["OWN_PEER"] = f"sip:c300x@{gw_domain}"   # destinatario dei frame OpenWebNet
    if DEVADDR:
        env["DEVADDR"] = DEVADDR
    proc = subprocess.Popen(["baresip", "-f", "/etc/baresip"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    def pump():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            # la registrazione compare subito nei log ("200 Registration successful"),
            # prima che arrivi l'evento REGISTER_OK: così lo stato è corretto già all'avvio
            if "Registration successful" in line:
                STATE["registered"] = True
                STATE["last_register"] = time.time()
            elif "Register:" in line and ("failed" in line or "error" in line.lower()):
                STATE["registered"] = False
            print("[baresip]", line, flush=True)
    threading.Thread(target=pump, daemon=True).start()

    try:
        control_loop()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
