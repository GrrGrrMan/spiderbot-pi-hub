#!/usr/bin/env bash
# pi-hub/scripts/05_setup_tailscale_https.sh
set -euo pipefail

echo "[Layer 5] Configuring Tailscale TLS & Ingress Gateway..."

if ! command -v tailscale >/dev/null 2>&1; then
    echo "[!] Tailscale CLI not found. Installing..."
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# 1. Reset any legacy background serve to avoid port 443 conflicts with Nginx
sudo tailscale serve --reset 2>/dev/null || true

# 2. Trigger official Let's Encrypt certificate retrieval
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${SCRIPT_DIR}/scripts/07_setup_tailscale_cert.sh" ]; then
    bash "${SCRIPT_DIR}/scripts/07_setup_tailscale_cert.sh"
fi

echo "[Layer 5] Tailscale TLS ingress managed directly by Nginx (Port 443)."