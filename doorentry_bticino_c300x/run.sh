#!/usr/bin/env bash
set -e

CONFIG=/data/options.json
export EMAIL=$(jq -r '.email' $CONFIG)
export PASSWORD=$(jq -r '.password' $CONFIG)
export DEVICE_NAME=$(jq -r '.device_name' $CONFIG)
export SNAPSHOT_PATH=$(jq -r '.snapshot_path' $CONFIG)
export HA_EVENT=$(jq -r '.ha_event' $CONFIG)
export SNAPSHOT_DELAY=$(jq -r '.snapshot_delay' $CONFIG)
export CAPTURE_MODE=$(jq -r '.capture_mode' $CONFIG)
export FALLBACK_DELAY=$(jq -r '.fallback_delay // 10' $CONFIG)
export HTTP_PORT=$(jq -r '.http_port' $CONFIG)
export WEBHOOK_ID=$(jq -r '.webhook_id // ""' $CONFIG)
export HA_BASE_URL=$(jq -r '.ha_base_url // "http://homeassistant:8123"' $CONFIG)
export FALLBACK_RECORD_DURATION=$(jq -r '.fallback_record_duration // 30' $CONFIG)
export DOORENTRY_SDP_DEVADDR=$(jq -r '.sdp_devaddr // "20"' $CONFIG)
export DOORENTRY_UA=$(jq -r '.sip_useragent // "VctLinphoneService/2.0.0"' $CONFIG)
export DEBUG_LOGS=$(jq -r '.debug_logs // false' $CONFIG)

# cartella moduli baresip: prima /usr/local/lib (baresip compilato da noi), poi /usr/lib
MODDIR=$(dirname "$(find /usr/local/lib /usr/lib -name 'account.so' -path '*baresip*' 2>/dev/null | head -1)")
if [ -z "$MODDIR" ]; then MODDIR=/usr/local/lib/baresip/modules; fi
export MODDIR
echo "[doorentry-bticino-c300x] moduli baresip in: $MODDIR"

mkdir -p /share/doorentry_bticino_c300x

# provisioning automatico: login, crea interno dedicato, certificati, DEVADDR, config baresip
echo "[doorentry-bticino-c300x] provisioning…"
if ! python3 /provision.py; then
  echo "[doorentry-bticino-c300x] provisioning fallito: controlla email/password nelle opzioni."
  exit 1
fi

echo "[doorentry-bticino-c300x] avvio supervisore"
exec python3 /doorbell.py
