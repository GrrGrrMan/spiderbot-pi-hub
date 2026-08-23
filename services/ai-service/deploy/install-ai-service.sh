#!/usr/bin/env bash
# services/ai-service/deploy/install-ai-service.sh
set -euo pipefail

APP=/opt/hexapod-ai
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="${SRC}/../../conf/ai.env"

echo "==> Installing OmniRoute AI Service from $SRC to $APP"
sudo mkdir -p "$APP" /etc/hexapod-ai
sudo cp "$SRC"/ai_service.py "$SRC"/action_parser.py "$SRC"/pipeline.py "$SRC"/embodied_agent.py "$SRC"/actions.json "$SRC"/animations.json "$APP"/
sudo cp -r "$SRC"/providers "$APP"/
sudo cp "$SRC"/requirements-ai.txt "$APP"/

# 1. Synchronize conf/ai.env to /etc/hexapod-ai/ai.env
if [ -f "$CONF_SRC" ]; then
    echo "==> Synchronizing configuration from conf/ai.env"
    sudo cp "$CONF_SRC" /etc/hexapod-ai/ai.env
elif [ ! -f /etc/hexapod-ai/ai.env ]; then
    echo "==> Writing fallback OmniRoute configuration"
    echo 'DEVICE_ID="hexapod-s3-01"' | sudo tee /etc/hexapod-ai/ai.env >/dev/null
    echo 'CAM_DEVICE_ID="hexapod-cam-01"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'LLM_ENABLED=1' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'LLM_BASE_URL="http://127.0.0.1:20128/v1"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'LLM_MODEL="deepseek/deepseek-chat"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'LLM_VISION_MODEL="minimax/minimax-01"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'LLM_API_KEY="spiderbot"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
    echo 'SNAPSHOT_URL="http://127.0.0.1:8088/snapshot"' | sudo tee -a /etc/hexapod-ai/ai.env >/dev/null
fi

# 2. Virtual environment setup
if [ ! -x "$APP/venv/bin/python" ]; then
  echo "==> Creating Python virtual environment"
  python3 -m venv "$APP/venv"
fi
sudo "$APP/venv/bin/pip" install --upgrade pip -q
sudo "$APP/venv/bin/pip" install -r "$APP/requirements-ai.txt" -q

# 3. Pre-download STT/TTS artifacts
sudo bash "$SRC/deploy/artifacts.sh"

# 4. Install & restart systemd unit
echo "==> Installing systemd unit"
sudo cp "$SRC/deploy/hexapod-ai.service" /etc/systemd/system/hexapod-ai.service
sudo chown -R spider:spider "$APP" /etc/hexapod-ai
sudo systemctl daemon-reload
sudo systemctl enable hexapod-ai
sudo systemctl restart hexapod-ai || true

sleep 2
systemctl --no-pager -l status hexapod-ai | head -12 || true
echo "==> OmniRoute AI Service ready."