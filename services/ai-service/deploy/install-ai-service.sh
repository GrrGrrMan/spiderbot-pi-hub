#!/usr/bin/env bash
# pi-hub/services/ai-service/deploy/install-ai-service.sh
# Idempotent installer for the P5 AI voice service on the RPi.
#   ssh spider@spiderbot-j.local  (or run on the Pi)
#   cd <repo>/pi-hub/services/ai-service
#   sudo ./deploy/install-ai-service.sh
set -euo pipefail

APP=/opt/hexapod-ai
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing AI service from $SRC to $APP"
sudo mkdir -p "$APP" /etc/hexapod-ai
sudo cp "$SRC"/ai_service.py "$SRC"/action_parser.py "$SRC"/pipeline.py "$SRC"/actions.json "$APP"/
sudo cp -r "$SRC"/providers "$APP"/
sudo cp "$SRC"/requirements-ai.txt "$APP"/

if [ ! -x "$APP/venv/bin/python" ]; then
  echo "==> Creating venv"
  python3 -m venv "$APP/venv"
fi
sudo "$APP/venv/bin/pip" install --upgrade pip -q
sudo "$APP/venv/bin/pip" install -r "$APP/requirements-ai.txt"

echo "==> Pre-downloading STT/TTS artifacts"
sudo bash "$SRC/deploy/artifacts.sh"

if [ ! -f /etc/hexapod-ai/groq.key ]; then
  echo ""
  echo "!!! No Groq key yet. Create it with:"
  echo "      sudo nano /etc/hexapod-ai/groq.key   (one line: sk-...)"
  echo "      sudo chown spider:spider /etc/hexapod-ai/groq.key   (service runs as spider)"
  echo "      sudo chmod 600 /etc/hexapod-ai/groq.key"
fi
if [ ! -f /etc/hexapod-ai/ai.env ]; then
  echo "DEVICE_ID=hexapod-s3-01" | sudo tee /etc/hexapod-ai/ai.env >/dev/null
fi

echo "==> Installing systemd unit"
sudo cp "$SRC/deploy/hexapod-ai.service" /etc/systemd/system/hexapod-ai.service
sudo systemctl daemon-reload
sudo systemctl enable hexapod-ai
sudo systemctl restart hexapod-ai || true
sleep 2
systemctl --no-pager -l status hexapod-ai | head -12 || true
echo "==> done."