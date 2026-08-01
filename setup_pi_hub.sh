#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load Hotspot Config
if [[ -f "${SCRIPT_DIR}/conf/hotspot.env" ]]; then
    source "${SCRIPT_DIR}/conf/hotspot.env"
else
    HOTSPOT_SSID="spiderlink"
    HOTSPOT_PASS="spiderbot"
    HOTSPOT_IFACE="*"
fi

echo "=========================================="
echo "       Setting up Hexapod Pi-Hub          "
echo "=========================================="

# 1. Unblock Wi-Fi & Install dependencies
echo "[1/4] Unblocking Wi-Fi & Installing dependencies..."
sudo rfkill unblock wifi || true
sudo apt-get update -qq
sudo apt-get install -y -qq mosquitto mosquitto-clients avahi-daemon network-manager

# 2. Deploy Mosquitto Config
echo "[2/4] Deploying conf/mosquitto.conf..."
if [[ -f "${SCRIPT_DIR}/conf/mosquitto.conf" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/mosquitto.conf" /etc/mosquitto/conf.d/pi_hub.conf
    echo "   -> Installed /etc/mosquitto/conf.d/pi_hub.conf"
else
    echo "   -> ERROR: conf/mosquitto.conf not found!"
    exit 1
fi

# 3. Setup Hotspot from conf/hotspot.env
echo "[3/4] Deploying Hotspot '${HOTSPOT_SSID}'..."
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-ipforward.conf > /dev/null
sudo sysctl -p /etc/sysctl.d/99-ipforward.conf > /dev/null
if ! nmcli connection show "${HOTSPOT_SSID}" >/dev/null 2>&1; then
    sudo nmcli connection add type wifi ifname "${HOTSPOT_IFACE}" con-name "${HOTSPOT_SSID}" autoconnect yes ssid "${HOTSPOT_SSID}"
    sudo nmcli connection modify "${HOTSPOT_SSID}" 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared
    sudo nmcli connection modify "${HOTSPOT_SSID}" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${HOTSPOT_PASS}"
    echo "   -> Hotspot '${HOTSPOT_SSID}' created!"
else
    echo "   -> Hotspot '${HOTSPOT_SSID}' already exists."
fi

# 4. Enable and Restart Services
echo "[4/4] Starting Mosquitto and mDNS services..."
sudo systemctl enable avahi-daemon mosquitto
sudo systemctl restart avahi-daemon mosquitto

echo "=========================================="
echo "   Pi-Hub Setup Complete!                 "
echo "   • MQTT Config : conf/mosquitto.conf    "
echo "   • Hotspot     : ${HOTSPOT_SSID}        "
echo "   • mDNS        : pi-hub.local           "
echo "=========================================="