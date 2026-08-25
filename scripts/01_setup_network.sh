#!/usr/bin/env bash
# pi-hub/scripts/01_setup_network.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${SCRIPT_DIR}/conf/hotspot.env"

echo "[Layer 1] Configuring Network Gateway & Hotspot '${HOTSPOT_SSID}'..."

sudo rfkill unblock wifi || true
sudo apt-get update -qq
sudo apt-get install -y -qq network-manager iptables-persistent netfilter-persistent

# 1. Enable IPv4 Kernel Forwarding
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-ipforward.conf > /dev/null
sudo sysctl -p /etc/sysctl.d/99-ipforward.conf > /dev/null

# 2. Configure AP Hotspot in NetworkManager
if ! nmcli connection show "${HOTSPOT_SSID}" >/dev/null 2>&1; then
    sudo nmcli connection add type wifi ifname "${HOTSPOT_IFACE}" con-name "${HOTSPOT_SSID}" autoconnect yes ssid "${HOTSPOT_SSID}"
    sudo nmcli connection modify "${HOTSPOT_SSID}" 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 192.168.4.1/24
    sudo nmcli connection modify "${HOTSPOT_SSID}" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${HOTSPOT_PASS}"
    echo "   -> Created Hotspot connection '${HOTSPOT_SSID}'"
else
    echo "   -> Hotspot connection '${HOTSPOT_SSID}' already exists."
fi

# 3. Configure Dynamic Subnet NAT Masquerading (Uplink Agnostic)
echo "   -> Configuring dynamic outbound NAT masquerading for Hotspot subnet (192.168.4.0/24)..."

sudo iptables -t nat -C POSTROUTING -s 192.168.4.0/24 ! -d 192.168.4.0/24 -j MASQUERADE 2>/dev/null || \
sudo iptables -t nat -A POSTROUTING -s 192.168.4.0/24 ! -d 192.168.4.0/24 -j MASQUERADE

sudo iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
sudo iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -C FORWARD -s 192.168.4.0/24 -j ACCEPT 2>/dev/null || \
sudo iptables -A FORWARD -s 192.168.4.0/24 -j ACCEPT

sudo netfilter-persistent save >/dev/null
echo "[Layer 1] Network Gateway setup complete."