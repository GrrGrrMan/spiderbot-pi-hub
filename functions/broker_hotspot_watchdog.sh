#!/usr/bin/env bash
set -euo pipefail

# Keeps the advertised SpiderBot hotspot aligned with broker health.
#
# If Mosquitto dies while spiderlink is up, the Pi first tries to repair
# Mosquitto. If the broker still cannot listen, the Pi stops hotspot.service so
# ESPs leave the dead AP and fall back to another whitelisted WiFi path.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${PI_HUB_CONFIG:-${SCRIPT_DIR}/../conf/pi_hub.conf}"

usage() {
  cat <<'USAGE'
Usage: broker_hotspot_watchdog.sh [--config <path>]

Checks Mosquitto and hotspot health once. Intended for a systemd timer.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      shift
      CONFIG_FILE="${1:?--config requires a file path}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[broker-watchdog] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

: "${PI_HUB_MQTT_PORT:=1883}"
: "${PI_HUB_ENABLE_HOTSPOT:=0}"
: "${PI_HUB_BROKER_WATCHDOG_ENABLE:=1}"
: "${PI_HUB_BROKER_WATCHDOG_RESTART_WAIT_SEC:=3}"
: "${PI_HUB_BROKER_WATCHDOG_STOP_HOTSPOT_ON_FAILURE:=1}"
: "${PI_HUB_BROKER_WATCHDOG_MARKER:=/run/spiderbot-broker-watchdog/hotspot-suppressed}"

if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

MQTT_PORT="${SPIDERBOT_MQTT_PORT:-${PI_HUB_MQTT_PORT:-1883}}"
MARKER="${PI_HUB_BROKER_WATCHDOG_MARKER:-/run/spiderbot-broker-watchdog/hotspot-suppressed}"
RESTART_WAIT_SEC="${PI_HUB_BROKER_WATCHDOG_RESTART_WAIT_SEC:-3}"

bool_on() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

log_msg() {
  echo "[broker-watchdog] $*"
  logger -t spiderbot-broker-watchdog -- "$*" 2>/dev/null || true
}

broker_healthy() {
  systemctl is-active --quiet mosquitto &&
    ss -ltn | grep -Eq ":${MQTT_PORT}[[:space:]]"
}

hotspot_active() {
  systemctl is-active --quiet hotspot
}

restart_broker() {
  log_msg "Mosquitto unhealthy; attempting restart"
  systemctl reset-failed mosquitto >/dev/null 2>&1 || true
  systemctl restart mosquitto || return 1
  sleep "${RESTART_WAIT_SEC}"
  broker_healthy
}

restore_hotspot_if_suppressed() {
  [[ -f "${MARKER}" ]] || return 0
  if hotspot_active; then
    rm -f "${MARKER}"
    return 0
  fi

  log_msg "Broker healthy again; restarting hotspot suppressed by watchdog"
  if systemctl start hotspot; then
    rm -f "${MARKER}"
  else
    log_msg "Hotspot restart failed; leaving suppression marker in place"
    return 1
  fi
}

if ! bool_on "${PI_HUB_BROKER_WATCHDOG_ENABLE:-1}"; then
  exit 0
fi

if ! bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
  exit 0
fi

if broker_healthy; then
  restore_hotspot_if_suppressed
  exit 0
fi

if restart_broker; then
  log_msg "Mosquitto recovered"
  restore_hotspot_if_suppressed
  exit 0
fi

if hotspot_active && bool_on "${PI_HUB_BROKER_WATCHDOG_STOP_HOTSPOT_ON_FAILURE:-1}"; then
  mkdir -p "$(dirname "${MARKER}")"
  {
    date -Is
    echo "broker_port=${MQTT_PORT}"
    echo "config=${CONFIG_FILE}"
  } >"${MARKER}"
  log_msg "Mosquitto still unhealthy; stopping hotspot so ESPs can leave dead broker path"
  systemctl stop hotspot || true
  exit 0
fi

log_msg "Mosquitto unhealthy and hotspot is not active"
exit 1
