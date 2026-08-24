#!/usr/bin/env bash
# pi-hub/scripts/05_setup_tailscale_https.sh
set -euo pipefail

echo "[Layer 5] Setting up Tailscale HTTPS Proxy (Port 443 -> Port 80)..."

if ! command -v tailscale >/dev/null 2>&1; then
    echo "[!] Tailscale CLI not found. Installing..."
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# 1. Start Tailscale background HTTPS serve for port 80
sudo tailscale serve --bg 80

# 2. Display Status & HTTPS URL
echo "============================================================"
sudo tailscale serve status || true
echo "============================================================"
echo "[Layer 5] Tailscale HTTPS active."