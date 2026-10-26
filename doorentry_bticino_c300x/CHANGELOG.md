# Changelog

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
