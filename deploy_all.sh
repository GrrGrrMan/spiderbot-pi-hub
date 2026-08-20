#!/usr/bin/env bash
# ==============================================================================
# Hexapod Pi-Hub Master Cleanup & Deployment Script
# Run on the Pi: bash deploy_all.sh
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/hexapod-ai"

echo "=========================================="
echo "    HEXAPOD PI-HUB CLEANUP & DEPLOYMENT   "
echo "=========================================="

# ── 1. Clean Home Directory Clutter ───────────────────────────────────────────
echo "[1/6] Cleaning up old loose files in /home/spider..."
cd /home/spider
rm -f ai_full_startup.txt ai_logs.txt ai_startup_logs.txt caddyfile.v2-patch mqtt.service nginx-spiderbot-v2.conf || true
echo "   -> Home directory cleaned."

# ── 2. System Packages & Dependencies ─────────────────────────────────────────
echo "[2/6] Ensuring system packages are installed..."
sudo apt-get update -qq
sudo apt-get install -y -qq mosquitto mosquitto-clients avahi-daemon nginx python3-venv python3-pip

# ── 3. Configure Mosquitto (Ports 1883 TCP & 9001 WebSockets) ─────────────────
echo "[3/6] Configuring Mosquitto MQTT..."
if [[ -f "${SCRIPT_DIR}/conf/mosquitto.conf" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/mosquitto.conf" /etc/mosquitto/conf.d/pi_hub.conf
    echo "   -> Installed /etc/mosquitto/conf.d/pi_hub.conf"
fi
sudo systemctl restart mosquitto
sudo systemctl enable mosquitto

# ── 4. Configure Avahi & NGINX ────────────────────────────────────────────────
echo "[4/6] Configuring mDNS & NGINX Web Server..."
if [[ -f "${SCRIPT_DIR}/conf/mqtt.service" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/mqtt.service" /etc/avahi/services/mqtt.service
fi
sudo systemctl restart avahi-daemon

if [[ -f "${SCRIPT_DIR}/conf/nginx-spiderbot-v2.conf" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/nginx-spiderbot-v2.conf" /etc/nginx/sites-available/default
    sudo nginx -t && sudo systemctl restart nginx
    echo "   -> NGINX configured for Web-UI at /home/spider/v2-web-ui/build"
fi

# ── 5. Deploy AI Voice & Companion Service ────────────────────────────────────
echo "[5/6] Deploying AI Service to ${APP_DIR}..."
sudo mkdir -p "${APP_DIR}" /etc/hexapod-ai

# Copy AI service files
sudo cp "${SCRIPT_DIR}/services/ai-service/ai_service.py" "${APP_DIR}/"
sudo cp "${SCRIPT_DIR}/services/ai-service/action_parser.py" "${APP_DIR}/"
sudo cp "${SCRIPT_DIR}/services/ai-service/pipeline.py" "${APP_DIR}/"
sudo cp "${SCRIPT_DIR}/services/ai-service/actions.json" "${APP_DIR}/"
sudo cp -r "${SCRIPT_DIR}/services/ai-service/providers" "${APP_DIR}/"

# Setup Python venv
if [ ! -d "${APP_DIR}/venv" ]; then
    echo "   -> Creating Python venv in ${APP_DIR}/venv..."
    sudo python3 -m venv "${APP_DIR}/venv"
fi
sudo "${APP_DIR}/venv/bin/pip" install --upgrade pip -q
sudo "${APP_DIR}/venv/bin/pip" install paho-mqtt openai faster-whisper piper-tts -q
echo "   -> Python dependencies installed."

# Systemd service unit
if [[ -f "${SCRIPT_DIR}/services/ai-service/deploy/hexapod-ai.service" ]]; then
    sudo cp "${SCRIPT_DIR}/services/ai-service/deploy/hexapod-ai.service" /etc/systemd/system/hexapod-ai.service
    sudo systemctl daemon-reload
    sudo systemctl enable hexapod-ai
    sudo systemctl restart hexapod-ai || true
fi

# ── 6. Verification & Health Diagnostic ───────────────────────────────────────
echo "[6/6] Verifying Pi-Hub Status..."
echo "------------------------------------------------------------"
sudo systemctl is-active mosquitto && echo "[✓] Mosquitto MQTT: ACTIVE" || echo "[!] Mosquitto MQTT: INACTIVE"
sudo systemctl is-active nginx && echo "[✓] NGINX Web Server: ACTIVE" || echo "[!] NGINX: INACTIVE"
sudo systemctl is-active avahi-daemon && echo "[✓] Avahi mDNS: ACTIVE" || echo "[!] Avahi: INACTIVE"
sudo systemctl is-active hexapod-ai && echo "[✓] AI Service (hexapod-ai): ACTIVE" || echo "[!] AI Service: INACTIVE"
echo "------------------------------------------------------------"
echo "Deployment Complete!"