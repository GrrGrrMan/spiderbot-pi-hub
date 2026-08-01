#!/usr/bin/env bash
set -euo pipefail
# setup_hotspot.sh — install/repair USB dongle hotspot on this Pi.
# Called by setup_pi_initial.sh or install_pi_hub.sh when PI_HUB_ENABLE_HOTSPOT=1.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${PI_HUB_CONFIG:-${SCRIPT_DIR}/../conf/pi_hub.conf}"

# ── Defaults ──────────────────────────────────────────────────────────────────
: "${PI_HUB_HOTSPOT_SSID:=spiderlink}"
: "${PI_HUB_HOTSPOT_PASS:=spiderbot}"
: "${PI_HUB_HOTSPOT_DONGLE_MAC:=}"
: "${PI_HUB_HOTSPOT_DONGLE_VENDOR:=}"
: "${PI_HUB_HOTSPOT_DONGLE_PRODUCT:=}"
: "${PI_HUB_HOTSPOT_AP_IFACE:=wlan-ap}"
: "${PI_HUB_HOTSPOT_NET_IFACE:=auto}"
: "${PI_HUB_HOTSPOT_IP:=192.168.4.1}"
: "${PI_HUB_HOTSPOT_DHCP_START:=192.168.4.10}"
: "${PI_HUB_HOTSPOT_DHCP_END:=192.168.4.50}"
: "${PI_HUB_HOTSPOT_COUNTRY:=NZ}"
: "${PI_HUB_HOTSPOT_CHANNEL:=6}"
: "${PI_HUB_HOTSPOT_AP_ISOLATE:=0}"
: "${PI_HUB_HOTSPOT_ENABLE_NAT:=1}"
: "${PI_HUB_HOTSPOT_NAT_WAIT_SEC:=25}"
: "${PI_HUB_HOTSPOT_DNS_MODE:=system}"
: "${PI_HUB_HOTSPOT_DNS1:=1.1.1.1}"
: "${PI_HUB_HOTSPOT_DNS2:=8.8.8.8}"

if [[ -f "${CONFIG_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${CONFIG_FILE}"
fi

bool_on() {
    case "${1:-0}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

# ── Validate required values ──────────────────────────────────────────────────
if [[ -z "${PI_HUB_HOTSPOT_DONGLE_MAC}" && ( -z "${PI_HUB_HOTSPOT_DONGLE_VENDOR}" || -z "${PI_HUB_HOTSPOT_DONGLE_PRODUCT}" ) ]]; then
    echo "[hotspot] ERROR: identify the AP dongle by vendor/product or by MAC." >&2
    echo "[hotspot] Preferred: set PI_HUB_HOTSPOT_DONGLE_VENDOR and PI_HUB_HOTSPOT_DONGLE_PRODUCT from lsusb." >&2
    echo "[hotspot] Fallback: set PI_HUB_HOTSPOT_DONGLE_MAC from ip link show." >&2
    exit 1
fi
if [[ "${#PI_HUB_HOTSPOT_PASS}" -lt 8 ]]; then
    echo "[hotspot] ERROR: PI_HUB_HOTSPOT_PASS must be at least 8 characters." >&2
    exit 1
fi
if [[ "${PI_HUB_HOTSPOT_AP_IFACE}" == "${PI_HUB_HOTSPOT_NET_IFACE}" ]]; then
    echo "[hotspot] ERROR: AP interface and upstream interface cannot be the same." >&2
    exit 1
fi

match_desc="vendor/product ${PI_HUB_HOTSPOT_DONGLE_VENDOR}:${PI_HUB_HOTSPOT_DONGLE_PRODUCT}"
if [[ -n "${PI_HUB_HOTSPOT_DONGLE_MAC}" ]]; then
    match_desc="MAC ${PI_HUB_HOTSPOT_DONGLE_MAC}"
fi
echo "[hotspot] Installing hotspot: SSID=${PI_HUB_HOTSPOT_SSID} AP=${PI_HUB_HOTSPOT_AP_IFACE} upstream=${PI_HUB_HOTSPOT_NET_IFACE} match=${match_desc}"

# ── 1. Install packages ───────────────────────────────────────────────────────
echo "[hotspot] Installing hostapd, dnsmasq, iptables-persistent, iw, usbutils..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y hostapd dnsmasq iptables-persistent iw usbutils
sudo systemctl disable hostapd 2>/dev/null || true
sudo systemctl disable dnsmasq  2>/dev/null || true
sudo systemctl mask dnsmasq

# ── 2. udev rename rule ───────────────────────────────────────────────────────
echo "[hotspot] Writing udev rename rule..."
if [[ -n "${PI_HUB_HOTSPOT_DONGLE_MAC}" ]]; then
    sudo tee /etc/udev/rules.d/70-spiderbot-wifi-name.rules > /dev/null << EOF_RULE
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="${PI_HUB_HOTSPOT_DONGLE_MAC}", NAME="${PI_HUB_HOTSPOT_AP_IFACE}"
EOF_RULE
else
    sudo tee /etc/udev/rules.d/70-spiderbot-wifi-name.rules > /dev/null << EOF_RULE
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="${PI_HUB_HOTSPOT_DONGLE_VENDOR}", ATTRS{idProduct}=="${PI_HUB_HOTSPOT_DONGLE_PRODUCT}", NAME="${PI_HUB_HOTSPOT_AP_IFACE}"
EOF_RULE
fi

# ── 3. NetworkManager — ignore the AP interface ───────────────────────────────
echo "[hotspot] Telling NetworkManager to ignore AP interface..."
sudo mkdir -p /etc/NetworkManager/conf.d
nm_unmanaged="interface-name:${PI_HUB_HOTSPOT_AP_IFACE}"
if [[ -n "${PI_HUB_HOTSPOT_DONGLE_MAC}" ]]; then
    nm_unmanaged="${nm_unmanaged};mac:${PI_HUB_HOTSPOT_DONGLE_MAC}"
fi
sudo tee /etc/NetworkManager/conf.d/99-spiderbot-unmanaged-ap.conf > /dev/null << EOF_NM
[keyfile]
unmanaged-devices=${nm_unmanaged}
EOF_NM
sudo systemctl restart NetworkManager

# ── 4. hostapd config ─────────────────────────────────────────────────────────
echo "[hotspot] Writing hostapd config..."
sudo tee /etc/hostapd/hostapd-ap.conf > /dev/null << EOF_HOSTAPD
interface=${PI_HUB_HOTSPOT_AP_IFACE}
driver=nl80211
ssid=${PI_HUB_HOTSPOT_SSID}
hw_mode=g
channel=${PI_HUB_HOTSPOT_CHANNEL}
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
ap_isolate=${PI_HUB_HOTSPOT_AP_ISOLATE}
wpa=2
wpa_passphrase=${PI_HUB_HOTSPOT_PASS}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
country_code=${PI_HUB_HOTSPOT_COUNTRY}
ieee80211n=1
EOF_HOSTAPD

# ── 5. dnsmasq config ─────────────────────────────────────────────────────────
echo "[hotspot] Writing dnsmasq config..."
sudo tee /etc/dnsmasq.d/hotspot.conf > /dev/null << EOF_DNSMASQ
interface=${PI_HUB_HOTSPOT_AP_IFACE}
bind-dynamic
dhcp-range=${PI_HUB_HOTSPOT_DHCP_START},${PI_HUB_HOTSPOT_DHCP_END},255.255.255.0,24h
dhcp-option=3,${PI_HUB_HOTSPOT_IP}
dhcp-option=6,${PI_HUB_HOTSPOT_IP}
EOF_DNSMASQ
if [[ "${PI_HUB_HOTSPOT_DNS_MODE}" == "static" ]]; then
    {
        echo "no-resolv"
        [[ -n "${PI_HUB_HOTSPOT_DNS1}" ]] && echo "server=${PI_HUB_HOTSPOT_DNS1}"
        [[ -n "${PI_HUB_HOTSPOT_DNS2}" ]] && echo "server=${PI_HUB_HOTSPOT_DNS2}"
    } | sudo tee -a /etc/dnsmasq.d/hotspot.conf > /dev/null
fi

# ── 6. start/stop scripts ─────────────────────────────────────────────────────
echo "[hotspot] Writing start/stop scripts..."
sudo tee /usr/local/bin/start-hotspot.sh > /dev/null << SCRIPT
#!/bin/bash
set -euo pipefail
LOG="/var/log/hotspot.log"
STATE_DIR="/run/spiderbot-hotspot"
LOCK_FILE="/run/spiderbot-hotspot.lock"
UPSTREAM_STATE="\$STATE_DIR/upstream"
HOSTAPD_PID="\$STATE_DIR/hostapd.pid"
DNSMASQ_PID="\$STATE_DIR/dnsmasq.pid"
AP_IFACE="${PI_HUB_HOTSPOT_AP_IFACE}"
NET_IFACE="${PI_HUB_HOTSPOT_NET_IFACE}"
AP_IP="${PI_HUB_HOTSPOT_IP}"
ENABLE_NAT="${PI_HUB_HOTSPOT_ENABLE_NAT}"
NAT_WAIT_SEC="${PI_HUB_HOTSPOT_NAT_WAIT_SEC}"
DONGLE_MAC="${PI_HUB_HOTSPOT_DONGLE_MAC}"
DONGLE_VENDOR="${PI_HUB_HOTSPOT_DONGLE_VENDOR}"
DONGLE_PRODUCT="${PI_HUB_HOTSPOT_DONGLE_PRODUCT}"

exec >> "\$LOG" 2>&1
mkdir -p "\$STATE_DIR"
exec 9>"\$LOCK_FILE"
if ! flock -n 9; then
    echo "\$(date) hotspot start already running; exiting"
    exit 0
fi

echo "=== \$(date) === Starting hotspot on \$AP_IFACE"
rm -f "\$UPSTREAM_STATE"

resolve_upstream() {
    if [[ "\$NET_IFACE" != "auto" && -n "\$NET_IFACE" ]]; then
        if ip link show "\$NET_IFACE" >/dev/null 2>&1; then
            echo "\$NET_IFACE"
            return 0
        fi
        return 1
    fi

    local dev
    dev="\$(ip route get 1.1.1.1 2>/dev/null | awk -v ap="\$AP_IFACE" '{for (i=1;i<=NF;i++) if (\$i=="dev" && \$(i+1)!=ap) {print \$(i+1); exit}}')"
    if [[ -n "\$dev" ]]; then
        echo "\$dev"
        return 0
    fi

    ip route show default 2>/dev/null | awk -v ap="\$AP_IFACE" '{for (i=1;i<=NF;i++) if (\$i=="dev" && \$(i+1)!=ap) {print \$(i+1); exit}}' | head -1
}

wait_for_upstream() {
    local waited=0 upstream=""
    while (( waited <= NAT_WAIT_SEC )); do
        upstream="\$(resolve_upstream || true)"
        if [[ -n "\$upstream" && "\$upstream" != "\$AP_IFACE" ]]; then
            echo "\$upstream"
            return 0
        fi
        sleep 1
        waited=\$((waited + 1))
    done
    return 1
}

cleanup_processes() {
    pkill -f "hostapd.*hostapd-ap.conf" 2>/dev/null || true
    [[ -f "\$HOSTAPD_PID" ]] && xargs -r kill <"\$HOSTAPD_PID" 2>/dev/null || true
    [[ -f "\$DNSMASQ_PID" ]] && xargs -r kill <"\$DNSMASQ_PID" 2>/dev/null || true
    cat /var/run/dnsmasq-hotspot.pid 2>/dev/null | xargs -r kill 2>/dev/null || true
    pkill -f "dnsmasq.*hotspot.conf" 2>/dev/null || true
    rm -f "\$HOSTAPD_PID" "\$DNSMASQ_PID" /var/run/dnsmasq-hotspot.pid
}

device_matches_ap_dongle() {
    local iface="\$1" props=""
    [[ "\$iface" == "lo" || "\$iface" == "\$AP_IFACE" ]] && return 1
    [[ "\$iface" == "tailscale"* ]] && return 1

    if [[ -n "\$DONGLE_MAC" ]]; then
        local addr=""
        addr="\$(cat "/sys/class/net/\$iface/address" 2>/dev/null || true)"
        [[ "\${addr,,}" == "\${DONGLE_MAC,,}" ]] && return 0
    fi

    if [[ -n "\$DONGLE_VENDOR" && -n "\$DONGLE_PRODUCT" ]] && command -v udevadm >/dev/null 2>&1; then
        props="\$(udevadm info -q property -p "/sys/class/net/\$iface" 2>/dev/null || true)"
        grep -qi "^ID_VENDOR_ID=\$DONGLE_VENDOR\$" <<<"\$props" &&
            grep -qi "^ID_MODEL_ID=\$DONGLE_PRODUCT\$" <<<"\$props" &&
            return 0
    fi

    return 1
}

find_ap_dongle_iface() {
    local iface
    for path in /sys/class/net/*; do
        iface="\${path##*/}"
        if device_matches_ap_dongle "\$iface"; then
            echo "\$iface"
            return 0
        fi
    done
    return 1
}

ensure_ap_iface() {
    if ip link show "\$AP_IFACE" >/dev/null 2>&1; then
        return 0
    fi

    local candidate=""
    candidate="\$(find_ap_dongle_iface || true)"
    if [[ -z "\$candidate" ]]; then
        return 1
    fi

    echo "Renaming AP dongle interface \$candidate -> \$AP_IFACE"
    nmcli dev disconnect "\$candidate" >/dev/null 2>&1 || true
    nmcli dev set "\$candidate" managed no >/dev/null 2>&1 || true
    ip link set "\$candidate" down || true
    ip link set "\$candidate" name "\$AP_IFACE"
}

cleanup_processes
systemctl stop dnsmasq 2>/dev/null || true
systemctl mask dnsmasq 2>/dev/null || true
rfkill unblock wifi 2>/dev/null || true
sleep 1

ensure_ap_iface || true

for i in \$(seq 1 30); do
    if iw dev "\$AP_IFACE" info > /dev/null 2>&1 || ip link show "\$AP_IFACE" >/dev/null 2>&1; then
        echo "Interface ready (attempt \$i)"
        break
    fi
    echo "Waiting for \$AP_IFACE... (\$i/30)"
    sleep 0.5
done

if ! ip link show "\$AP_IFACE" >/dev/null 2>&1; then
    echo "ERROR: \$AP_IFACE never became ready"
    exit 1
fi

ip link set "\$AP_IFACE" up
ip addr flush dev "\$AP_IFACE"
ip addr add "\$AP_IP/24" dev "\$AP_IFACE"

if [[ "\$ENABLE_NAT" != "0" ]]; then
    UPSTREAM="\$(wait_for_upstream || true)"
    if [[ -n "\$UPSTREAM" && "\$UPSTREAM" != "\$AP_IFACE" ]]; then
        echo 1 > /proc/sys/net/ipv4/ip_forward
        iptables -t nat -C POSTROUTING -o "\$UPSTREAM" -j MASQUERADE 2>/dev/null || \
            iptables -t nat -A POSTROUTING -o "\$UPSTREAM" -j MASQUERADE
        iptables -C FORWARD -i "\$AP_IFACE" -o "\$UPSTREAM" -j ACCEPT 2>/dev/null || \
            iptables -A FORWARD -i "\$AP_IFACE" -o "\$UPSTREAM" -j ACCEPT
        iptables -C FORWARD -i "\$UPSTREAM" -o "\$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
            iptables -A FORWARD -i "\$UPSTREAM" -o "\$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
        echo "\$UPSTREAM" > "\$UPSTREAM_STATE"
        echo "NAT enabled: \$AP_IFACE -> \$UPSTREAM"
    else
        echo "WARN: no valid upstream detected after \${NAT_WAIT_SEC}s; hotspot will be local-only"
    fi
else
    echo "NAT disabled; hotspot is local-only"
fi

hostapd -B -P "\$HOSTAPD_PID" /etc/hostapd/hostapd-ap.conf
sleep 2

dnsmasq --conf-file=/etc/dnsmasq.d/hotspot.conf --pid-file="\$DNSMASQ_PID"
ln -sf "\$DNSMASQ_PID" /var/run/dnsmasq-hotspot.pid 2>/dev/null || true

if ! pgrep -f "hostapd.*hostapd-ap.conf" >/dev/null 2>&1; then
    echo "ERROR: hostapd did not stay running"
    exit 1
fi
if ! pgrep -f "dnsmasq.*hotspot.conf" >/dev/null 2>&1; then
    echo "ERROR: dnsmasq did not stay running"
    exit 1
fi

echo "Hotspot running. SSID: ${PI_HUB_HOTSPOT_SSID}"
SCRIPT

sudo tee /usr/local/bin/stop-hotspot.sh > /dev/null << SCRIPT
#!/bin/bash
set -euo pipefail
AP_IFACE="${PI_HUB_HOTSPOT_AP_IFACE}"
NET_IFACE="${PI_HUB_HOTSPOT_NET_IFACE}"
STATE_DIR="/run/spiderbot-hotspot"
LOCK_FILE="/run/spiderbot-hotspot.lock"
UPSTREAM_STATE="\$STATE_DIR/upstream"
HOSTAPD_PID="\$STATE_DIR/hostapd.pid"
DNSMASQ_PID="\$STATE_DIR/dnsmasq.pid"
LOG="/var/log/hotspot.log"

mkdir -p "\$STATE_DIR"
exec 9>"\$LOCK_FILE"
flock -n 9 || exit 0
exec >> "\$LOG" 2>&1

echo "=== \$(date) === Stopping hotspot on \$AP_IFACE"

resolve_upstream() {
    if [[ "\$NET_IFACE" != "auto" && -n "\$NET_IFACE" ]]; then
        echo "\$NET_IFACE"
        return 0
    fi
    if [[ -f "\$UPSTREAM_STATE" ]]; then
        cat "\$UPSTREAM_STATE"
        return 0
    fi
    ip route get 1.1.1.1 2>/dev/null | awk -v ap="\$AP_IFACE" '{for (i=1;i<=NF;i++) if (\$i=="dev" && \$(i+1)!=ap) {print \$(i+1); exit}}'
}

[[ -f "\$HOSTAPD_PID" ]] && xargs -r kill <"\$HOSTAPD_PID" 2>/dev/null || true
[[ -f "\$DNSMASQ_PID" ]] && xargs -r kill <"\$DNSMASQ_PID" 2>/dev/null || true
cat /var/run/dnsmasq-hotspot.pid 2>/dev/null | xargs -r kill 2>/dev/null || true
pkill -f "hostapd.*hostapd-ap.conf" 2>/dev/null || true
pkill -f "dnsmasq.*hotspot.conf" 2>/dev/null || true
rm -f "\$HOSTAPD_PID" "\$DNSMASQ_PID" /var/run/dnsmasq-hotspot.pid

if ip link show "\$AP_IFACE" >/dev/null 2>&1; then
    ip addr flush dev "\$AP_IFACE" 2>/dev/null || true
fi

UPSTREAM="\$(resolve_upstream || true)"
if [[ -n "\$UPSTREAM" && "\$UPSTREAM" != "\$AP_IFACE" ]]; then
    while iptables -t nat -C POSTROUTING -o "\$UPSTREAM" -j MASQUERADE 2>/dev/null; do
        iptables -t nat -D POSTROUTING -o "\$UPSTREAM" -j MASQUERADE || break
    done
    while iptables -C FORWARD -i "\$AP_IFACE" -o "\$UPSTREAM" -j ACCEPT 2>/dev/null; do
        iptables -D FORWARD -i "\$AP_IFACE" -o "\$UPSTREAM" -j ACCEPT || break
    done
    while iptables -C FORWARD -i "\$UPSTREAM" -o "\$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; do
        iptables -D FORWARD -i "\$UPSTREAM" -o "\$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT || break
    done
fi
rm -rf "\$STATE_DIR" 2>/dev/null || true
echo "Hotspot stopped"
SCRIPT

sudo chmod +x /usr/local/bin/start-hotspot.sh
sudo chmod +x /usr/local/bin/stop-hotspot.sh

# ── 7. systemd services ───────────────────────────────────────────────────────
echo "[hotspot] Writing systemd service..."
sudo tee /etc/systemd/system/hotspot.service > /dev/null << EOF_SERVICE
[Unit]
Description=WiFi Hotspot on USB Dongle (${PI_HUB_HOTSPOT_AP_IFACE})
Wants=network-online.target
After=network-online.target NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sleep 5
ExecStart=/usr/local/bin/start-hotspot.sh
ExecStop=/usr/local/bin/stop-hotspot.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF_SERVICE

echo "[hotspot] Writing replug helper service..."
sudo tee /etc/systemd/system/hotspot-replug@.service > /dev/null << 'EOF_REPLUG'
[Unit]
Description=Restart SpiderBot hotspot after USB WiFi replug (%i)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 2
ExecStartPre=/bin/udevadm settle
ExecStart=/bin/systemctl restart hotspot.service
EOF_REPLUG


echo "[hotspot] Writing remove helper service..."
sudo tee /etc/systemd/system/hotspot-remove@.service > /dev/null << 'EOF_REMOVE'
[Unit]
Description=Stop SpiderBot hotspot after USB WiFi removal (%i)

[Service]
Type=oneshot
ExecStart=/bin/systemctl stop hotspot.service
EOF_REMOVE

# ── 8. udev trigger rule ──────────────────────────────────────────────────────
echo "[hotspot] Writing udev hotspot trigger..."
if [[ -n "${PI_HUB_HOTSPOT_DONGLE_VENDOR}" && -n "${PI_HUB_HOTSPOT_DONGLE_PRODUCT}" ]]; then
    sudo tee /etc/udev/rules.d/99-spiderbot-usb-wifi-hotspot.rules > /dev/null << EOF_TRIGGER
ACTION=="add", SUBSYSTEM=="net", ATTRS{idVendor}=="${PI_HUB_HOTSPOT_DONGLE_VENDOR}", ATTRS{idProduct}=="${PI_HUB_HOTSPOT_DONGLE_PRODUCT}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-replug@%k.service"
ACTION=="move", SUBSYSTEM=="net", ATTRS{idVendor}=="${PI_HUB_HOTSPOT_DONGLE_VENDOR}", ATTRS{idProduct}=="${PI_HUB_HOTSPOT_DONGLE_PRODUCT}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-replug@%k.service"
ACTION=="remove", SUBSYSTEM=="net", ATTRS{idVendor}=="${PI_HUB_HOTSPOT_DONGLE_VENDOR}", ATTRS{idProduct}=="${PI_HUB_HOTSPOT_DONGLE_PRODUCT}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-remove@%k.service"
EOF_TRIGGER
else
    sudo tee /etc/udev/rules.d/99-spiderbot-usb-wifi-hotspot.rules > /dev/null << EOF_TRIGGER
ACTION=="add", SUBSYSTEM=="net", ATTR{address}=="${PI_HUB_HOTSPOT_DONGLE_MAC}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-replug@%k.service"
ACTION=="move", SUBSYSTEM=="net", ATTR{address}=="${PI_HUB_HOTSPOT_DONGLE_MAC}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-replug@%k.service"
ACTION=="remove", SUBSYSTEM=="net", ATTR{address}=="${PI_HUB_HOTSPOT_DONGLE_MAC}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="hotspot-remove@%k.service"
EOF_TRIGGER
fi

# ── 9. sysctl persistence ─────────────────────────────────────────────────────
if bool_on "${PI_HUB_HOTSPOT_ENABLE_NAT}"; then
    echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-spiderbot-ipforward.conf > /dev/null
fi

# ── 10. Reload everything and start if possible ───────────────────────────────
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl reset-failed 'hotspot-replug@*.service' >/dev/null 2>&1 || true
sudo sysctl --system >/dev/null || true
sudo systemctl enable hotspot >/dev/null 2>&1 || true

if ip link show "${PI_HUB_HOTSPOT_AP_IFACE}" >/dev/null 2>&1 || iw dev "${PI_HUB_HOTSPOT_AP_IFACE}" info >/dev/null 2>&1; then
    echo "[hotspot] AP interface present; restarting hotspot now..."
    sudo systemctl restart hotspot || true
else
    echo "[hotspot] AP interface not present. Plug/replug the dongle or run: sudo systemctl restart hotspot"
fi

echo "[hotspot] Done. Replug auto-restart is handled by hotspot-replug@.service."
