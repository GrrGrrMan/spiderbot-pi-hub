#!/usr/bin/env bash
# pi-hub/scripts/07_setup_tailscale_cert.sh
set -euo pipefail

echo "[Tailscale] Checking Tailscale TLS status..."

sudo mkdir -p /etc/ssl/spiderbot

# 1. Fallback placeholder so Nginx can always start even if offline
if [ ! -f /etc/ssl/spiderbot/tailscale.crt ] && [ -f /etc/ssl/spiderbot/selfsigned.crt ]; then
    sudo cp /etc/ssl/spiderbot/selfsigned.crt /etc/ssl/spiderbot/tailscale.crt
    sudo cp /etc/ssl/spiderbot/selfsigned.key /etc/ssl/spiderbot/tailscale.key
fi

# 2. Extract Tailscale domain cleanly using Python
TS_DOMAIN=$(tailscale status --json 2>/dev/null | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('Self', {}).get('DNSName', '').rstrip('.'))" || true)

if [ -n "$TS_DOMAIN" ]; then
    echo "   -> Found Tailscale FQDN: ${TS_DOMAIN}"
    echo "   -> Fetching Let's Encrypt TLS certificate..."
    if sudo tailscale cert --cert-file /etc/ssl/spiderbot/tailscale.crt --key-file /etc/ssl/spiderbot/tailscale.key "${TS_DOMAIN}"; then
        sudo chmod 600 /etc/ssl/spiderbot/tailscale.key
        sudo chmod 644 /etc/ssl/spiderbot/tailscale.crt
        echo "[Tailscale] Official certificate installed for ${TS_DOMAIN}"
    else
        echo "[!] Warning: 'tailscale cert' failed. Ensure HTTPS Certificates is enabled in your Tailscale Admin Console (DNS tab)."
        echo "    (Using local fallback certificate in the meantime)"
    fi
else
    echo "[!] Notice: Tailscale MagicDNS domain not detected or Tailscale is offline."
    echo "    (Using local fallback certificate for local LAN & Hotspot)"
fi