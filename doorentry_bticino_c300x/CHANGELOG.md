# Changelog

## 1.12.0
Prima versione pubblica. Funzionalità principali:
- Provisioning automatico da sole email e password dell'app Door Entry: l'add-on crea un interno
  SIP dedicato, ottiene password e certificati, e scopre la configurazione dell'impianto.
- Notifica allo squillo verso Home Assistant (via webhook, senza token).
- Cattura di uno screenshot dell'ingresso in modalità `fallback_snapshot` (quando nessuno risponde
  entro `fallback_delay` secondi) oppure solo notifica in `notify_only`.
- Discovery automatico delle serrature dalla configurazione (apertura per ciascuna).
- Comandi: apertura serrature, luce scale, segreteria (risponditore).
- Pagina di stato web sulla porta 8099 e API per l'integrazione Home Assistant.
