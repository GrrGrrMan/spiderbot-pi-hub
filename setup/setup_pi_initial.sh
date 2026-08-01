#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-${PI_HUB_CONFIG:-${SCRIPT_DIR}/../conf/pi_hub.conf}}"
CONFIG_FILE="$(cd "$(dirname "${CONFIG_FILE}")" && pwd)/$(basename "${CONFIG_FILE}")"

: "${PI_HUB_ENABLE_MULTI_WIFI:=1}"
: "${PI_HUB_APPLY_SYSTEM_SETUP:=0}"
: "${PI_HUB_HOSTNAME:=}"
: "${PI_HUB_CREATE_USER:=0}"
: "${PI_HUB_USER:=spider}"
: "${PI_HUB_USER_PASSWORD:=}"
: "${PI_HUB_STATIC_CONNECTION:=}"
: "${PI_HUB_STATIC_IPV4:=}"
: "${PI_HUB_STATIC_GATEWAY:=}"
: "${PI_HUB_STATIC_DNS:=1.1.1.1,8.8.8.8}"
: "${PI_HUB_WIFI_IFACE:=wlan0}"
declare -a PI_WIFI_PERSONAL_NETWORKS=()
declare -a PI_WIFI_ENTERPRISE_NETWORKS=()

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "[pi-initial] Missing ${CONFIG_FILE}." >&2
  echo "[pi-initial] Copy Rpi/conf/pi_hub.conf.example to pi_hub.conf first, or pass the config path." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${CONFIG_FILE}"

bool_on() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
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
  else
    if [[ -n "${PI_HUB_TAILSCALE_AUTH_KEY:-}" ]]; then
      echo "[pi-hub] Bringing Tailscale up with auth key..."
      local up_args=()
      up_args+=("--auth-key=${PI_HUB_TAILSCALE_AUTH_KEY}")

      if [[ -n "${PI_HUB_TAILSCALE_HOSTNAME:-}" ]]; then
        up_args+=("--hostname=${PI_HUB_TAILSCALE_HOSTNAME}")
      fi

      if [[ -n "${PI_HUB_TAILSCALE_EXTRA_ARGS:-}" ]]; then
        # shellcheck disable=SC2206
        up_args+=(${PI_HUB_TAILSCALE_EXTRA_ARGS})
      fi

      sudo tailscale up "${up_args[@]}"
    else
      echo "[pi-hub] Tailscale installed but not logged in."
      echo "[pi-hub] Run manually on this Pi:"
      echo "  sudo tailscale up"
    fi
  fi

  echo "[pi-hub] Tailscale verification:"
  if command -v tailscale >/dev/null 2>&1; then
    tailscale status 2>/dev/null | head -20 || true
    echo "[pi-hub] Tailscale IPv4:"
    tailscale ip -4 2>/dev/null || true
  else
    echo "[pi-hub] Tailscale: NOT installed"
  fi
}

sanitize_name() {
  printf '%s' "$1" | tr -cs '[:alnum:]_-' '-' | sed -E 's/^-+|-+$//g'
}

need_nmcli() {
  if ! command -v nmcli >/dev/null 2>&1; then
    echo "[pi-initial] nmcli not found. Install/use modern Raspberry Pi OS with NetworkManager." >&2
    exit 1
  fi
}

connection_exists() {
  nmcli -t -f NAME connection show | grep -Fxq "$1"
}

set_common_wifi_options() {
  local con="$1"
  local priority="$2"
  sudo nmcli connection modify "$con" \
    connection.autoconnect yes \
    connection.autoconnect-priority "${priority:-50}" \
    ipv4.method auto \
    ipv6.method auto
}

configure_personal_wifi() {
  local row="$1"
  local name ssid password priority
  IFS='|' read -r name ssid password priority <<<"${row}"
  if [[ -z "${name}" || -z "${ssid}" || -z "${password}" ]]; then
    echo "[pi-initial] Skipping personal Wi-Fi row with missing name/ssid/password"
    return
  fi

  local con="spiderbot-$(sanitize_name "${name}")"
  echo "[pi-initial] Configuring personal Wi-Fi '${ssid}' as ${con}"
  if ! connection_exists "${con}"; then
    sudo nmcli connection add type wifi ifname "${PI_HUB_WIFI_IFACE}" con-name "${con}" ssid "${ssid}"
  fi
  sudo nmcli connection modify "${con}" \
    802-11-wireless.ssid "${ssid}" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "${password}"
  set_common_wifi_options "${con}" "${priority:-50}"
}

configure_enterprise_wifi() {
  local row="$1"
  local name ssid anonymous_identity username password eap phase2 ca_cert priority
  IFS='|' read -r name ssid anonymous_identity username password eap phase2 ca_cert priority <<<"${row}"
  if [[ -z "${name}" || -z "${ssid}" || -z "${username}" || -z "${password}" ]]; then
    echo "[pi-initial] Skipping enterprise Wi-Fi row with missing name/ssid/username/password"
    return
  fi

  eap="${eap:-PEAP}"
  phase2="${phase2:-MSCHAPV2}"
  local eap_lc="${eap,,}"
  local phase2_lc="${phase2,,}"
  local con="spiderbot-$(sanitize_name "${name}")"

  echo "[pi-initial] Configuring enterprise Wi-Fi '${ssid}' as ${con} (${eap}/${phase2})"
  if ! connection_exists "${con}"; then
    sudo nmcli connection add type wifi ifname "${PI_HUB_WIFI_IFACE}" con-name "${con}" ssid "${ssid}"
  fi

  sudo nmcli connection modify "${con}" \
    802-11-wireless.ssid "${ssid}" \
    wifi-sec.key-mgmt wpa-eap \
    802-1x.eap "${eap_lc}" \
    802-1x.phase2-auth "${phase2_lc}" \
    802-1x.identity "${username}" \
    802-1x.password "${password}"

  if [[ -n "${anonymous_identity}" ]]; then
    sudo nmcli connection modify "${con}" 802-1x.anonymous-identity "${anonymous_identity}"
  fi
  if [[ -n "${ca_cert}" ]]; then
    sudo nmcli connection modify "${con}" 802-1x.ca-cert "${ca_cert}"
  fi

  set_common_wifi_options "${con}" "${priority:-50}"
}

apply_static_ip() {
  if [[ -z "${PI_HUB_STATIC_IPV4}" ]]; then
    return
  fi

  local con="${PI_HUB_STATIC_CONNECTION}"
  if [[ -z "${con}" && "${#PI_WIFI_PERSONAL_NETWORKS[@]}" -gt 0 ]]; then
    local first_name
    IFS='|' read -r first_name _ <<<"${PI_WIFI_PERSONAL_NETWORKS[0]}"
    con="spiderbot-$(sanitize_name "${first_name}")"
  fi
  if [[ -z "${con}" ]]; then
    echo "[pi-initial] Static IP requested, but PI_HUB_STATIC_CONNECTION is empty." >&2
    exit 1
  fi

  echo "[pi-initial] Applying static IPv4 ${PI_HUB_STATIC_IPV4} to ${con}"
  sudo nmcli connection modify "${con}" \
    ipv4.method manual \
    ipv4.addresses "${PI_HUB_STATIC_IPV4}"
  if [[ -n "${PI_HUB_STATIC_GATEWAY}" ]]; then
    sudo nmcli connection modify "${con}" ipv4.gateway "${PI_HUB_STATIC_GATEWAY}"
  fi
  if [[ -n "${PI_HUB_STATIC_DNS}" ]]; then
    sudo nmcli connection modify "${con}" ipv4.dns "${PI_HUB_STATIC_DNS}"
  fi
}

need_nmcli

if bool_on "${PI_HUB_ENABLE_MULTI_WIFI}"; then
  sudo nmcli radio wifi on
  for row in "${PI_WIFI_PERSONAL_NETWORKS[@]}"; do
    configure_personal_wifi "${row}"
  done
  for row in "${PI_WIFI_ENTERPRISE_NETWORKS[@]}"; do
    configure_enterprise_wifi "${row}"
  done
else
  echo "[pi-initial] Multi-Wi-Fi configuration skipped by PI_HUB_ENABLE_MULTI_WIFI=0"
fi

if bool_on "${PI_HUB_APPLY_SYSTEM_SETUP}"; then
  if [[ -n "${PI_HUB_HOSTNAME}" && "$(hostname)" != "${PI_HUB_HOSTNAME}" ]]; then
    echo "[pi-initial] Setting hostname to ${PI_HUB_HOSTNAME}"
    sudo hostnamectl set-hostname "${PI_HUB_HOSTNAME}"
  fi

  if bool_on "${PI_HUB_CREATE_USER}"; then
    if ! id -u "${PI_HUB_USER}" >/dev/null 2>&1; then
      echo "[pi-initial] Creating sudo user ${PI_HUB_USER}"
      sudo useradd -m -G sudo -s /bin/bash "${PI_HUB_USER}"
    fi
    if [[ -n "${PI_HUB_USER_PASSWORD}" ]]; then
      printf '%s:%s\n' "${PI_HUB_USER}" "${PI_HUB_USER_PASSWORD}" | sudo chpasswd
    else
      echo "[pi-initial] User password unchanged because PI_HUB_USER_PASSWORD is empty"
    fi
  fi

  apply_static_ip
else
  echo "[pi-initial] Hostname/user/static-IP setup skipped by PI_HUB_APPLY_SYSTEM_SETUP=0"
fi

install_tailscale_if_enabled

sudo nmcli connection reload
echo "[pi-initial] Configured NetworkManager profiles:"
nmcli -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show | sed -n '1p;/spiderbot-/p'

# ── Hotspot setup ─────────────────────────────────────────────────────────────
if bool_on "${PI_HUB_ENABLE_HOTSPOT:-0}"; then
    echo "[pi-initial] Setting up USB dongle hotspot..."
    PI_HUB_CONFIG="${CONFIG_FILE}" bash "${SCRIPT_DIR}/../functions/setup_hotspot.sh"
else
    echo "[pi-initial] Hotspot skipped (PI_HUB_ENABLE_HOTSPOT=0)"
fi

echo "[pi-initial] Done. Reboot only if hotspot/system networking changed and you need services to rebind."
