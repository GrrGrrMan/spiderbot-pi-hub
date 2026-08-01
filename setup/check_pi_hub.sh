#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${PI_HUB_CONFIG:-${SCRIPT_DIR}/../conf/pi_hub.conf}"

: "${PI_HUB_MQTT_USER:=spiderbot}"
: "${PI_HUB_MQTT_PASSWORD:=}"
: "${PI_HUB_MQTT_PORT:=1883}"
: "${PI_HUB_DISCOVERY_SECONDS:=8}"

if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

MQTT_USER="${SPIDERBOT_MQTT_USER:-${PI_HUB_MQTT_USER:-spiderbot}}"
MQTT_PASSWORD="${SPIDERBOT_MQTT_PASSWORD:-${PI_HUB_MQTT_PASSWORD:-}}"
MQTT_PORT="${SPIDERBOT_MQTT_PORT:-${PI_HUB_MQTT_PORT:-1883}}"
DISCOVERY_SECONDS="${PI_HUB_DISCOVERY_SECONDS:-8}"

echo "[pi-check] Host: $(hostname)"

if systemctl is-active --quiet mosquitto; then
  echo "[pi-check] Mosquitto: active"
else
  echo "[pi-check] Mosquitto: NOT active" >&2
  exit 1
fi

if ss -ltn | grep -q ":${MQTT_PORT} "; then
  echo "[pi-check] Port ${MQTT_PORT}: listening"
else
  echo "[pi-check] Port ${MQTT_PORT}: NOT listening" >&2
  exit 1
fi

if systemctl list-unit-files spiderbot-broker-watchdog.timer --no-legend 2>/dev/null |
    grep -q '^spiderbot-broker-watchdog\.timer'; then
  if systemctl is-active --quiet spiderbot-broker-watchdog.timer; then
    echo "[pi-check] Broker/hotspot watchdog: active"
  else
    echo "[pi-check] Broker/hotspot watchdog: installed but not active"
  fi
fi

if [[ -z "${MQTT_PASSWORD}" ]]; then
  echo "[pi-check] MQTT password not supplied; skipping ESP discovery."
  echo "[pi-check] Re-run with SPIDERBOT_MQTT_PASSWORD='...' or put it in pi_hub.conf."
  exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT

echo "[pi-check] Listening ${DISCOVERY_SECONDS}s for ESP state/event/log/light topics..."
set +e
timeout "${DISCOVERY_SECONDS}" mosquitto_sub \
  -h 127.0.0.1 \
  -p "${MQTT_PORT}" \
  -u "${MQTT_USER}" \
  -P "${MQTT_PASSWORD}" \
  -t 'alphaesp32/+/state' \
  -t 'alphaesp32/+/event' \
  -t 'alphaesp32/+/log' \
  -t 'alphaesp32s3/+/state' \
  -t 'alphaesp32s3/+/event' \
  -t 'alphaesp32s3/+/log' \
  -t 'spiderbot/s3/health' \
  -t 'spiderbot/s3/capabilities' \
  -t 'spiderbot/s3/lights/+/state' \
  -v >"${tmp}" &
sub_pid=$!
sleep 1
mosquitto_pub \
  -h 127.0.0.1 \
  -p "${MQTT_PORT}" \
  -u "${MQTT_USER}" \
  -P "${MQTT_PASSWORD}" \
  -t 'alphaesp32/spiderbot01/cmd/discrete' \
  -m status >/dev/null 2>&1 || true
mosquitto_pub \
  -h 127.0.0.1 \
  -p "${MQTT_PORT}" \
  -u "${MQTT_USER}" \
  -P "${MQTT_PASSWORD}" \
  -t 'alphaesp32s3/spiderbot-s3/cmd/discrete' \
  -m status >/dev/null 2>&1 || true
mosquitto_pub \
  -h 127.0.0.1 \
  -p "${MQTT_PORT}" \
  -u "${MQTT_USER}" \
  -P "${MQTT_PASSWORD}" \
  -t 'alphaesp32s3/spiderbot-s3/cmd/discrete' \
  -m light:health >/dev/null 2>&1 || true
mosquitto_pub \
  -h 127.0.0.1 \
  -p "${MQTT_PORT}" \
  -u "${MQTT_USER}" \
  -P "${MQTT_PASSWORD}" \
  -t 'alphaesp32s3/spiderbot-s3/cmd/discrete' \
  -m light:capabilities >/dev/null 2>&1 || true
wait "${sub_pid}"
rc=$?
set -e
if [[ "${rc}" -ne 0 && "${rc}" -ne 124 ]]; then
  echo "[pi-check] Discovery subscribe failed with code ${rc}" >&2
  exit "${rc}"
fi

mapfile -t roots < <(
  awk '{print $1}' "${tmp}" |
    awk -F/ 'NF >= 3 && ($1 == "alphaesp32" || $1 == "alphaesp32s3") { print $1 "/" $2 }' |
    sort -u
)

case "${#roots[@]}" in
  0)
    echo "[pi-check] WARNING: no ESP devices observed."
    echo "[pi-check] Check ESP Wi-Fi scan standby, local control port 7777, broker credentials, ACLs, and spiderbot-esp-link."
    echo "[pi-check] Try: Rpi/functions/link_esp_to_pi.sh --check"
    ;;
  1)
    echo "[pi-check] WARNING: one ESP observed: ${roots[0]}"
    echo "[pi-check] That is okay for now; make sure the remote controller target matches this root."
    ;;
  *)
    echo "[pi-check] ESP devices observed:"
    printf '  - %s\n' "${roots[@]}"
    ;;
esac
