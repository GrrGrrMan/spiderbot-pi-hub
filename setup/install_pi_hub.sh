#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUNCTIONS_DIR="$(cd "${SCRIPT_DIR}/../functions" && pwd)"
CONF_DIR="$(cd "${SCRIPT_DIR}/../conf" && pwd)"
CONFIG_FILE="${PI_HUB_CONFIG:-${CONF_DIR}/pi_hub.conf}"
VERIFY_ONLY=0
RECONFIGURE=0
SKIP_APT=0

usage() {
  cat <<'USAGE'
Usage: Rpi/setup/install_pi_hub.sh [options]

Installs or repairs the SpiderBot Pi hub: Mosquitto, optional hotspot,
optional Tailscale, and optional local ESP link scanner.

Options:
  --verify-only       Check installed hub services and exit.
  --reconfigure       Rewrite Mosquitto password/config and hotspot files.
  --force             Alias for --reconfigure.
  --skip-apt          Do not run apt update/install.
  --config <path>     Use a specific pi_hub.conf.
  -h, --help          Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify-only)
      VERIFY_ONLY=1
      ;;
    --reconfigure|--force)
      RECONFIGURE=1
      ;;
    --skip-apt)
      SKIP_APT=1
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
      echo "[pi-hub] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

CONFIG_FILE="$(cd "$(dirname "${CONFIG_FILE}")" && pwd)/$(basename "${CONFIG_FILE}")"

: "${PI_HUB_MQTT_USER:=spiderbot}"
: "${PI_HUB_MQTT_PASSWORD:=}"
: "${PI_HUB_MQTT_PORT:=1883}"
: "${PI_HUB_ESP_LINK_ENABLE:=1}"
: "${PI_HUB_TAILSCALE_ENABLE:=0}"
: "${PI_HUB_TAILSCALE_AUTH_KEY:=}"
: "${PI_HUB_TAILSCALE_HOSTNAME:=}"
: "${PI_HUB_TAILSCALE_EXTRA_ARGS:=}"
: "${PI_HUB_ENABLE_HOTSPOT:=0}"
: "${PI_HUB_HOTSPOT_AP_IFACE:=wlan-ap}"
: "${PI_HUB_HOTSPOT_IP:=192.168.4.1}"
: "${PI_HUB_BROKER_WATCHDOG_ENABLE:=1}"
: "${PI_HUB_BROKER_WATCHDOG_INTERVAL_SEC:=30}"

if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

MQTT_USER="${SPIDERBOT_MQTT_USER:-${PI_HUB_MQTT_USER:-spiderbot}}"
MQTT_PASSWORD="${SPIDERBOT_MQTT_PASSWORD:-${PI_HUB_MQTT_PASSWORD:-}}"
MQTT_PORT="${SPIDERBOT_MQTT_PORT:-${PI_HUB_MQTT_PORT:-1883}}"
HOTSPOT_AP_IFACE="${PI_HUB_HOTSPOT_AP_IFACE:-wlan-ap}"
HOTSPOT_IP="${PI_HUB_HOTSPOT_IP:-192.168.4.1}"

bool_on() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

esp_link_enabled() {
  [[ "${PI_HUB_ESP_LINK_ENABLE:-${PI_HUB_AUTO_LINK_ENABLE:-1}}" != "0" && -n "${PI_HUB_LINK_TOKEN:-}" ]]
}

broker_watchdog_enabled() {
  bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}" && bool_on "${PI_HUB_BROKER_WATCHDOG_ENABLE:-1}"
}

has_existing_hub() {
  command -v mosquitto >/dev/null 2>&1 &&
    [[ -f /etc/mosquitto/conf.d/spiderbot.conf ]] &&
    [[ -f /etc/mosquitto/passwd ]] &&
    systemctl is-active --quiet mosquitto
}

hotspot_identity_configured() {
  [[ -n "${PI_HUB_HOTSPOT_DONGLE_MAC:-}" ]] ||
    [[ -n "${PI_HUB_HOTSPOT_DONGLE_VENDOR:-}" && -n "${PI_HUB_HOTSPOT_DONGLE_PRODUCT:-}" ]]
}

hotspot_dongle_present() {
  if [[ -n "${PI_HUB_HOTSPOT_DONGLE_VENDOR:-}" && -n "${PI_HUB_HOTSPOT_DONGLE_PRODUCT:-}" ]]; then
    if command -v lsusb >/dev/null 2>&1; then
      lsusb | grep -qi "${PI_HUB_HOTSPOT_DONGLE_VENDOR}:${PI_HUB_HOTSPOT_DONGLE_PRODUCT}" && return 0
    else
      echo "[pi-hub] lsusb not available; cannot check hotspot dongle by vendor/product" >&2
    fi
  fi

  if [[ -n "${PI_HUB_HOTSPOT_DONGLE_MAC:-}" ]]; then
    ip link | grep -qi "${PI_HUB_HOTSPOT_DONGLE_MAC}" && return 0
  fi

  ip link show "${HOTSPOT_AP_IFACE}" >/dev/null 2>&1 && return 0
  return 1
}

hotspot_installed() {
  [[ -f /etc/systemd/system/hotspot.service ]] &&
    [[ -f /etc/systemd/system/hotspot-replug@.service ]] &&
    [[ -x /usr/local/bin/start-hotspot.sh ]] &&
    [[ -x /usr/local/bin/stop-hotspot.sh ]] &&
    [[ -f /etc/hostapd/hostapd-ap.conf ]] &&
    [[ -f /etc/dnsmasq.d/hotspot.conf ]] &&
    [[ -f /etc/udev/rules.d/99-spiderbot-usb-wifi-hotspot.rules ]]
}

verify_hub() {
  local ok=0
  echo "[pi-hub] Verification:"

  if command -v mosquitto >/dev/null 2>&1 || [[ -x /usr/sbin/mosquitto ]]; then
    echo "  ok: mosquitto installed"
  else
    echo "  missing: mosquitto"
    ok=1
  fi

  if command -v mosquitto_pub >/dev/null 2>&1 || [[ -x /usr/bin/mosquitto_pub ]]; then
    echo "  ok: mosquitto-clients installed"
  else
    echo "  missing: mosquitto-clients"
    ok=1
  fi

  if systemctl is-enabled --quiet mosquitto; then
    echo "  ok: mosquitto enabled"
  else
    echo "  warn: mosquitto is not enabled"
    ok=1
  fi

  if systemctl is-active --quiet mosquitto; then
    echo "  ok: mosquitto active"
  else
    echo "  missing: mosquitto is not active"
    ok=1
  fi

  if ss -ltn | grep -Eq ":${MQTT_PORT}[[:space:]]"; then
    echo "  ok: broker listening on ${MQTT_PORT}"
  else
    echo "  missing: broker not listening on ${MQTT_PORT}"
    ok=1
  fi

  if [[ -f /etc/mosquitto/conf.d/spiderbot.conf ]]; then
    echo "  ok: /etc/mosquitto/conf.d/spiderbot.conf present"
  else
    echo "  missing: /etc/mosquitto/conf.d/spiderbot.conf"
    ok=1
  fi

  if [[ -f /etc/mosquitto/acl.d/spiderbot.acl ]]; then
    echo "  ok: /etc/mosquitto/acl.d/spiderbot.acl present"
  else
    echo "  missing: /etc/mosquitto/acl.d/spiderbot.acl"
    ok=1
  fi

  if [[ -f /etc/mosquitto/passwd ]]; then
    if stat_output="$(stat -c '%U:%G %a %n' /etc/mosquitto/passwd 2>/dev/null)"; then
      echo "  ok: ${stat_output}"
    else
      echo "  ok: /etc/mosquitto/passwd present"
    fi
  else
    echo "  missing: /etc/mosquitto/passwd"
    ok=1
  fi

  if esp_link_enabled; then
    if [[ -f /etc/systemd/system/spiderbot-esp-link.service ]]; then
      echo "  ok: spiderbot-esp-link service installed"
    else
      echo "  missing: spiderbot-esp-link service"
      ok=1
    fi

    if systemctl is-active --quiet spiderbot-esp-link; then
      echo "  ok: spiderbot-esp-link active"
    else
      echo "  warn: spiderbot-esp-link is not active"
      ok=1
    fi
  elif [[ "${PI_HUB_ESP_LINK_ENABLE:-${PI_HUB_AUTO_LINK_ENABLE:-1}}" != "0" ]]; then
    echo "  warn: spiderbot-esp-link skipped because PI_HUB_LINK_TOKEN is empty"
  fi

  if bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
    if hotspot_installed; then
      echo "  ok: hotspot files installed"
    else
      echo "  missing: hotspot install files are incomplete"
      ok=1
    fi

    if systemctl is-enabled --quiet hotspot; then
      echo "  ok: hotspot service enabled"
    else
      echo "  warn: hotspot service is not enabled"
      ok=1
    fi

    if [[ -f /etc/systemd/system/hotspot-replug@.service ]]; then
      echo "  ok: hotspot replug helper installed"
    else
      echo "  missing: hotspot replug helper"
      ok=1
    fi

    if hotspot_dongle_present; then
      echo "  ok: hotspot dongle/interface present"
      if ip addr show "${HOTSPOT_AP_IFACE}" 2>/dev/null | grep -q "${HOTSPOT_IP}/"; then
        echo "  ok: ${HOTSPOT_AP_IFACE} has ${HOTSPOT_IP}"
      else
        echo "  warn: ${HOTSPOT_AP_IFACE} does not have ${HOTSPOT_IP}"
        ok=1
      fi

      if pgrep -f 'hostapd.*hostapd-ap.conf' >/dev/null 2>&1; then
        echo "  ok: hostapd running"
      else
        echo "  warn: hostapd is not running"
        ok=1
      fi

      if pgrep -f 'dnsmasq.*hotspot.conf' >/dev/null 2>&1; then
        echo "  ok: hotspot dnsmasq running"
      else
        echo "  warn: hotspot dnsmasq is not running"
        ok=1
      fi

      if broker_watchdog_enabled; then
        if [[ -x /usr/local/bin/spiderbot-broker-watchdog.sh ]]; then
          echo "  ok: broker/hotspot watchdog script installed"
        else
          echo "  missing: broker/hotspot watchdog script"
          ok=1
        fi

        if [[ -f /etc/systemd/system/spiderbot-broker-watchdog.service &&
              -f /etc/systemd/system/spiderbot-broker-watchdog.timer ]]; then
          echo "  ok: broker/hotspot watchdog units installed"
        else
          echo "  missing: broker/hotspot watchdog systemd units"
          ok=1
        fi

        if systemctl is-enabled --quiet spiderbot-broker-watchdog.timer; then
          echo "  ok: broker/hotspot watchdog timer enabled"
        else
          echo "  warn: broker/hotspot watchdog timer is not enabled"
          ok=1
        fi

        if systemctl is-active --quiet spiderbot-broker-watchdog.timer; then
          echo "  ok: broker/hotspot watchdog timer active"
        else
          echo "  warn: broker/hotspot watchdog timer is not active"
          ok=1
        fi
      fi
    else
      echo "  warn: hotspot configured but AP dongle/interface is not currently present"
    fi
  fi

  return "${ok}"
}

install_packages() {
  if [[ "${SKIP_APT}" -eq 1 ]]; then
    echo "[pi-hub] Skipping apt install/update by --skip-apt"
    return 0
  fi

  echo "[pi-hub] Installing hub dependencies..."
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    mosquitto \
    mosquitto-clients \
    python3 \
    python3-venv \
    python3-pip \
    python3-paho-mqtt \
    git \
    curl \
    avahi-daemon \
    hostapd \
    dnsmasq \
    iptables-persistent \
    iw \
    usbutils
}

install_tailscale_if_enabled() {
  if ! bool_on "${PI_HUB_TAILSCALE_ENABLE:-0}"; then
    echo "[pi-hub] Tailscale skipped by PI_HUB_TAILSCALE_ENABLE=0"
    return 0
  fi

  echo "[pi-hub] Checking Tailscale..."

  if ! command -v tailscale >/dev/null 2>&1; then
    echo "[pi-hub] Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
  else
    echo "[pi-hub] Tailscale already installed"
  fi

  sudo systemctl enable --now tailscaled >/dev/null 2>&1 || true

  if tailscale status >/dev/null 2>&1; then
    echo "[pi-hub] Tailscale already up"
  elif [[ -n "${PI_HUB_TAILSCALE_AUTH_KEY:-}" ]]; then
    echo "[pi-hub] Bringing Tailscale up with auth key..."
    local up_args=("--auth-key=${PI_HUB_TAILSCALE_AUTH_KEY}")
    [[ -n "${PI_HUB_TAILSCALE_HOSTNAME:-}" ]] && up_args+=("--hostname=${PI_HUB_TAILSCALE_HOSTNAME}")
    if [[ -n "${PI_HUB_TAILSCALE_EXTRA_ARGS:-}" ]]; then
      # shellcheck disable=SC2206
      up_args+=(${PI_HUB_TAILSCALE_EXTRA_ARGS})
    fi
    sudo tailscale up "${up_args[@]}"
  else
    echo "[pi-hub] Tailscale installed but not logged in."
    echo "[pi-hub] Run: sudo tailscale up"
  fi

  echo "[pi-hub] Tailscale IPv4:"
  tailscale ip -4 2>/dev/null || true
}

install_hotspot_if_enabled() {
  if ! bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
    echo "[pi-hub] Hotspot skipped by PI_HUB_ENABLE_HOTSPOT=0"
    return 0
  fi

  if ! hotspot_identity_configured; then
    echo "[pi-hub] Hotspot enabled, but no dongle identity is configured." >&2
    echo "[pi-hub] Set PI_HUB_HOTSPOT_DONGLE_VENDOR/PRODUCT or PI_HUB_HOTSPOT_DONGLE_MAC." >&2
    return 1
  fi

  if hotspot_installed && [[ "${RECONFIGURE}" -eq 0 ]]; then
    echo "[pi-hub] Hotspot already installed; repairing service/udev files and restarting if present..."
  else
    echo "[pi-hub] Installing/reconfiguring hotspot..."
  fi

  PI_HUB_CONFIG="${CONFIG_FILE}" bash "${FUNCTIONS_DIR}/setup_hotspot.sh"

  sudo systemctl enable hotspot >/dev/null 2>&1 || true
  if hotspot_dongle_present; then
    sudo systemctl restart hotspot || true
  else
    echo "[pi-hub] Hotspot dongle is not present now; udev replug helper will restart hotspot when it appears."
  fi
}

install_esp_link_service() {
  sudo systemctl disable --now spiderbot-auto-link >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/spiderbot-auto-link.service

  if esp_link_enabled; then
    echo "[pi-hub] Installing local ESP link scanner..."
    sudo tee /etc/systemd/system/spiderbot-esp-link.service >/dev/null <<EOF_SERVICE
[Unit]
Description=SpiderBot local ESP-to-Pi MQTT linker
Wants=network-online.target
After=network-online.target mosquitto.service

[Service]
Type=simple
WorkingDirectory=${FUNCTIONS_DIR}
ExecStart=/usr/bin/python3 ${FUNCTIONS_DIR}/esp_link_scanner.py --config ${CONFIG_FILE}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF_SERVICE
    sudo systemctl daemon-reload
    sudo systemctl enable --now spiderbot-esp-link
  else
    if [[ "${PI_HUB_ESP_LINK_ENABLE:-${PI_HUB_AUTO_LINK_ENABLE:-1}}" == "0" ]]; then
      echo "[pi-hub] Local ESP link scanner disabled by PI_HUB_ESP_LINK_ENABLE=0"
    else
      echo "[pi-hub] Local ESP link scanner skipped until PI_HUB_LINK_TOKEN is set"
    fi
    sudo systemctl disable --now spiderbot-esp-link >/dev/null 2>&1 || true
    sudo systemctl daemon-reload
  fi
}

install_broker_watchdog_service() {
  if broker_watchdog_enabled; then
    echo "[pi-hub] Installing broker/hotspot watchdog..."
    sudo install -m 0755 -o root -g root \
      "${FUNCTIONS_DIR}/broker_hotspot_watchdog.sh" \
      /usr/local/bin/spiderbot-broker-watchdog.sh

    sudo tee /etc/systemd/system/spiderbot-broker-watchdog.service >/dev/null <<EOF_SERVICE
[Unit]
Description=SpiderBot broker/hotspot watchdog
Wants=network-online.target
After=network-online.target mosquitto.service hotspot.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/spiderbot-broker-watchdog.sh --config ${CONFIG_FILE}
EOF_SERVICE

    sudo tee /etc/systemd/system/spiderbot-broker-watchdog.timer >/dev/null <<EOF_TIMER
[Unit]
Description=Run SpiderBot broker/hotspot watchdog

[Timer]
OnBootSec=45s
OnUnitActiveSec=${PI_HUB_BROKER_WATCHDOG_INTERVAL_SEC:-30}s
AccuracySec=5s
Unit=spiderbot-broker-watchdog.service

[Install]
WantedBy=timers.target
EOF_TIMER

    sudo systemctl daemon-reload
    sudo systemctl enable --now spiderbot-broker-watchdog.timer
  else
    echo "[pi-hub] Broker/hotspot watchdog disabled"
    sudo systemctl disable --now spiderbot-broker-watchdog.timer >/dev/null 2>&1 || true
    sudo systemctl disable --now spiderbot-broker-watchdog.service >/dev/null 2>&1 || true
    sudo systemctl daemon-reload
  fi
}

install_mosquitto_config() {
  if [[ -z "${MQTT_PASSWORD}" ]]; then
    read -r -s -p "MQTT password for ${MQTT_USER}: " MQTT_PASSWORD
    echo
  fi

  if [[ -z "${MQTT_PASSWORD}" ]]; then
    echo "MQTT password cannot be empty." >&2
    exit 1
  fi

  echo "[pi-hub] Installing SpiderBot Mosquitto config on port ${MQTT_PORT}..."
  sudo systemctl stop mosquitto >/dev/null 2>&1 || true
  sudo systemctl reset-failed mosquitto >/dev/null 2>&1 || true
  sudo install -d -m 0755 /etc/mosquitto/acl.d
  sudo install -d -m 0755 -o mosquitto -g mosquitto /var/lib/mosquitto
  sed -E "s/^listener[[:space:]]+[0-9]+[[:space:]].*/listener ${MQTT_PORT} 0.0.0.0/" \
    "${CONF_DIR}/mosquitto-spiderbot.conf" |
    sudo tee /etc/mosquitto/conf.d/spiderbot.conf >/dev/null
  sudo chown root:root /etc/mosquitto/conf.d/spiderbot.conf
  sudo chmod 0644 /etc/mosquitto/conf.d/spiderbot.conf
  sudo install -m 0644 -o root -g root \
    "${CONF_DIR}/spiderbot.acl" \
    /etc/mosquitto/acl.d/spiderbot.acl

  echo "[pi-hub] Creating/updating MQTT password file..."
  sudo touch /etc/mosquitto/passwd
  sudo mosquitto_passwd -b /etc/mosquitto/passwd "${MQTT_USER}" "${MQTT_PASSWORD}"
  sudo chown root:mosquitto /etc/mosquitto/passwd
  sudo chmod 640 /etc/mosquitto/passwd

  echo "[pi-hub] Validating Mosquitto config..."
  set +e
  sudo timeout 3 mosquitto -c /etc/mosquitto/mosquitto.conf -v
  local rc=$?
  set -e
  if [[ "${rc}" -ne 0 && "${rc}" -ne 124 ]]; then
    echo "[pi-hub] Mosquitto config validation failed." >&2
    exit "${rc}"
  fi

  echo "[pi-hub] Enabling services..."
  sudo systemctl enable --now avahi-daemon
  sudo systemctl enable mosquitto
  sudo systemctl restart mosquitto
}

if [[ "${VERIFY_ONLY}" -eq 1 ]]; then
  verify_hub
  exit $?
fi

install_packages
install_tailscale_if_enabled

if has_existing_hub && [[ "${RECONFIGURE}" -eq 0 ]]; then
  echo "[pi-hub] Existing active SpiderBot broker detected."
  echo "[pi-hub] Skipping Mosquitto password/config rewrite. Use --reconfigure to rewrite it."
  sudo systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
else
  install_mosquitto_config
fi

install_hotspot_if_enabled
install_esp_link_service
install_broker_watchdog_service

echo "[pi-hub] Broker status:"
systemctl --no-pager --full status mosquitto || true
if esp_link_enabled; then
  echo "[pi-hub] ESP link scanner status:"
  systemctl --no-pager --full status spiderbot-esp-link || true
fi
if bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
  echo "[pi-hub] Hotspot status:"
  systemctl --no-pager --full status hotspot || true
  if broker_watchdog_enabled; then
    echo "[pi-hub] Broker/hotspot watchdog status:"
    systemctl --no-pager --full status spiderbot-broker-watchdog.timer || true
  fi
fi

echo
echo "[pi-hub] Local broker is ready on port ${MQTT_PORT}."
echo "[pi-hub] MQTT user: ${MQTT_USER}"
echo "[pi-hub] Local-first mode:"
echo "  ESP local candidates should include:"
echo "    - hotspot SSID spiderlink -> ${HOTSPOT_IP}:${MQTT_PORT}"
echo "    - normal WiFi -> ESP local control port, then signed Pi link"
echo "  Hotspot replug helper: hotspot-replug@.service"
echo "  ESP MQTT username/password must match this broker."
if bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
  echo "  Hotspot replug auto-restart: enabled via hotspot-replug@.service"
  if broker_watchdog_enabled; then
    echo "  Broker/hotspot watchdog: enabled"
  fi
fi
if esp_link_enabled; then
  echo "  Local ESP link scanner: enabled"
else
  echo "  Local ESP link scanner: disabled until PI_HUB_LINK_TOKEN is set"
fi
echo "[pi-hub] Test from the Pi:"
echo "  mosquitto_pub -h 127.0.0.1 -p ${MQTT_PORT} -u ${MQTT_USER} -P '<password>' -t alphaesp32s3/spiderbot-s3/cmd/discrete -m status"
echo "  Rpi/functions/link_esp_to_pi.sh --check"
