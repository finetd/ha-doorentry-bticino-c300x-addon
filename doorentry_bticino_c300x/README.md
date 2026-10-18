# Add-on: Door Entry BTicino C300X

Add-on di Home Assistant che collega il videocitofono BTicino/Legrand Classe 300X (app *Door
Entry*) al cloud via SIP. Riceve gli squilli, cattura gli screenshot, invia i comandi al citofono
ed espone un'API HTTP locale usata dall'[integrazione](../custom_components/doorentry_bticino_c300x/).

## Installazione
1. Copia questa cartella in `addons/` (o `local_apps/`) di Home Assistant.
2. Add-on Store → ⋮ → *Controlla aggiornamenti* → installa.
3. In **Configurazione** imposta `email` e `password` dell'app Door Entry. Avvia.

Nei log, al primo avvio, devi vedere: `provisioning completato`, `200 Registration successful`
e (se l'impianto ha serrature) `discovery serrature: N`.

## Opzioni
| Opzione | Default | Descrizione |
|---|---|---|
| `email` / `password` | — | Credenziali dell'app Door Entry. |
| `device_name` | `Home Assistant` | Nome dell'interno dedicato creato sull'impianto. |
| `capture_mode` | `notify_only` | `notify_only` = solo notifica; `fallback_snapshot` = se nessuno risponde, cattura. |
| `fallback_delay` | `20` | Secondi di attesa prima della cattura di riserva (deve stare sotto il tempo di squillo del citofono). |
| `snapshot_delay` | `2.0` | Attesa del primo fotogramma dopo aver risposto. |
| `snapshot_path` | `/share/doorentry_bticino_c300x/doorbell.jpg` | Dove salvare l'ultima istantanea. |
| `webhook_id` | `doorentry_bticino_c300x` | ID del webhook HA per la notifica (senza token). |
| `ha_base_url` | `http://homeassistant:8123` | Base URL di HA per il webhook. |
| `http_port` | `8099` | Porta dell'API/pagina di stato. |

## Modalità di cattura
- **notify_only** (sicura): non risponde mai, lancia solo la notifica. Telefoni e posti interni
  squillano normalmente. Nessuna immagine nuova a ogni squillo.
- **fallback_snapshot**: allo squillo attende `fallback_delay` secondi; se nessuno ha risposto e
  la chiamata è ancora in corso, accetta, cattura l'immagine e riattacca. Se qualcuno risponde
  prima, non interviene.

## API HTTP (porta `http_port`)
- `/` — pagina di stato (pulsante "Visita").
- `/state` — stato in JSON (usato dall'integrazione).
- `/snapshot` — ultima istantanea JPEG.
- `/cmd/light`, `/cmd/answering_on`, `/cmd/answering_off` — comandi.
- `/cmd/lock/<PE>` — apre la serratura con quell'indirizzo.
- `/cmd/raw?f=<frame>` — invia un frame OpenWebNet (debug).

## Note
- Serve una base Debian: il file `build.yaml` la imposta. Il primo build compila baresip con il
  codec G.729 e il modulo per i comandi, quindi è lungo.
- Certificati e chiavi stanno nella cartella privata `/data`; in `/share` solo l'istantanea.
