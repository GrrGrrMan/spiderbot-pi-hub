#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${PI_HUB_CONFIG:-${SCRIPT_DIR}/../conf/pi_hub.conf}"
RUN_CHECK=0
DRY_RUN=0
declare -a SCANNER_ARGS=("--once")

usage() {
  cat <<'EOF'
Usage: Rpi/functions/link_esp_to_pi.sh [options]

Runs one local LAN discovery pass and sends signed link offers directly to ESPs'
local control port. This does not use public MQTT.

Options:
  --target <root>       Only link one target root, e.g. alphaesp32s3/spiderbot-s3.
  --host <ip>           Probe one ESP IP directly in addition to LAN candidates.
  --ttl <seconds>       Override link TTL seconds.
  --check               Run check_pi_hub.sh after linking.
  --dry-run             Show link offers without sending.
  --config <path>       Use a specific pi_hub.conf.
  -h, --help            Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      shift
      SCANNER_ARGS+=("--target" "${1:?--target requires a root}")
      ;;
    --host)
      shift
      SCANNER_ARGS+=("--host" "${1:?--host requires an IP}")
      ;;
    --ttl)
      shift
      SCANNER_ARGS+=("--ttl" "${1:?--ttl requires seconds}")
      ;;
    --check)
      RUN_CHECK=1
      ;;
    --dry-run)
      DRY_RUN=1
      SCANNER_ARGS+=("--dry-run")
      ;;
    --config)
      shift
      CONFIG_FILE="${1:?--config requires a file path}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[pi-link] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

echo "[pi-link] Local LAN link mode (no public MQTT)"
python3 "${SCRIPT_DIR}/esp_link_scanner.py" --config "${CONFIG_FILE}" "${SCANNER_ARGS[@]}"

if [[ "${RUN_CHECK}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
  echo "[pi-link] Waiting briefly for ESPs to reconnect locally..."
  sleep 3
  "${SCRIPT_DIR}/../setup/check_pi_hub.sh"
fi
