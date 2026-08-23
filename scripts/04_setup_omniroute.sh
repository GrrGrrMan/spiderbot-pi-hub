#!/usr/bin/env bash
# pi-hub/scripts/04_setup_omniroute.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="${SCRIPT_DIR}/conf/omniroute.env"
SERVICE_SRC="${SCRIPT_DIR}/services/omniroute/deploy/omniroute.service"

echo "[Layer 4] Installing & Configuring OmniRoute AI Gateway..."

# 1. Ensure directories exist
sudo mkdir -p /opt/omniroute /etc/omniroute
sudo chown -R spider:spider /opt/omniroute /etc/omniroute

# 2. Deploy default omniroute.env if not present
if [ ! -f /etc/omniroute/omniroute.env ]; then
    if [ -f "$CONF_SRC" ]; then
        echo "   -> Copying default omniroute.env to /etc/omniroute/"
        sudo cp "$CONF_SRC" /etc/omniroute/omniroute.env
    else
        echo 'OMNIROUTE_PORT=20128' | sudo tee /etc/omniroute/omniroute.env >/dev/null
        echo 'OMNIROUTE_API_KEY="spiderbot"' | sudo tee -a /etc/omniroute/omniroute.env >/dev/null
    fi
fi

# 3. Check if OmniRoute binary exists, otherwise install/guide
if ! command -v omniroute >/dev/null 2>&1; then
    echo "   -> Installing OmniRoute binary/CLI..."
    if command -v npm >/dev/null 2>&1; then
        sudo npm install -g omniroute || true
    elif command -v pip3 >/dev/null 2>&1; then
        sudo pip3 install omniroute --break-system-packages || true
    fi
fi

# 4. Install & Enable Systemd Unit
if [ -f "$SERVICE_SRC" ]; then
    sudo cp "$SERVICE_SRC" /etc/systemd/system/omniroute.service
    sudo systemctl daemon-reload
    sudo systemctl enable omniroute
    sudo systemctl restart omniroute || true
    echo "[Layer 4] OmniRoute active on http://0.0.0.0:20128/v1"
else
    echo "   [!] Warning: omniroute.service unit not found at $SERVICE_SRC"
fi