# Changelog

## 1.30.3

- `last.mp4` ora e' un symlink all'ultima registrazione in assoluto, non solo all'ultima
  on-demand. Si aggiorna sia dopo `/cmd/record` sia dopo fallback_video. Utile per card e
  automation che vogliono puntare a un path fisso senza sapere il nome timestampato
  (es: `platform: ffmpeg` con `input: /share/doorentry_bticino_c300x/recordings/last.mp4`).
- Lo stesso vale per `last.jpg` (thumbnail). Entrambi i symlink sono esclusi da
  `GET /recordings` per non apparire duplicati nel browser Media di HA.

## 1.30.2

- Al termine del muxing MP4 viene estratta anche una thumbnail JPG (primo frame) accanto
  al video, usata dall'integrazione per mostrare l'anteprima nella Camera entity.
- Endpoint `/recordings/<nome>` ora serve sia MP4 che JPG con Content-Type corretto.

## 1.30.1

- Nuovi endpoint HTTP: `GET /recordings` (lista MP4 con metadati) e
  `GET /recordings/<nome>` (serve il singolo file). Blocca path traversal.
- Il campo `last_recording` (nome, size, mtime) ora e' incluso in `/state` per
  l'integrazione, che espone un sensor "Ultima registrazione".

## 1.30.0

Prima release stabile.

### Funzionalita'

- Provisioning automatico tramite credenziali del portale `myhomeweb.com`: scarica
  configurazione cifrata, estrae certificati, costruisce account SIP.
- Registrazione SIP sul cloud BTicino via Flexisip (TLS mutuo, SRTP).
- Discovery automatico delle serrature dal `conf.xml` dell'impianto.
- Eventi su Home Assistant alla pressione del campanello.
- Pagina di stato HTML su porta 8099 con controlli (segreteria, serrature, luci, cattura).
- Entita' esposte all'integrazione: binary_sensor di stato chiamata, camera dello
  snapshot, button serrature e luci dinamici, switch segreteria, button cattura
  istantanea, button registrazione on-demand.
- Comandi HTTP: `/`, `/state`, `/snapshot`, `/info`, `/cmd/light`,
  `/cmd/answering_on`, `/cmd/answering_off`, `/cmd/lock/<PE>`, `/cmd/raw`,
  `/cmd/capture`, `/cmd/record?duration=N`, `/debug/conf`.
- Modalita' `capture_mode`:
    - `notify_only` (default): solo evento HA, mai risponde
    - `fallback_snapshot`: dopo `fallback_delay` secondi senza risposta, accetta e cattura foto
    - `fallback_video`: dopo `fallback_delay` secondi senza risposta, accetta e registra
      audio+video per `fallback_record_duration` secondi
- Registrazione on-demand video via `/cmd/record` (il citofono BTicino limita l'invio
  dell'audio in questa modalita'; audio funziona nelle chiamate reali in arrivo,
  usate da `fallback_video`).
- Opzione `debug_logs` per abilitare log diagnostici dettagliati on-demand.
