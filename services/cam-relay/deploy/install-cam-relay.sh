#!/usr/bin/env bash
# pi-hub/services/cam-relay/deploy/install-cam-relay.sh
set -euo pipefail

APP=/opt/hexapod-cam-relay
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="${SRC}/../../conf/cam_relay.env"

echo "==> Installing Camera Relay service from $SRC to $APP"
sudo mkdir -p "$APP" /etc/hexapod-cam-relay
sudo cp "$SRC"/cam_relay.py "$APP"/
sudo cp "$SRC"/requirements.txt "$APP"/

# Single Point of Truth: Copy default configuration from conf/cam_relay.env
if [ ! -f /etc/hexapod-cam-relay/cam_relay.env ]; then
    echo "==> Deploying configuration from conf/cam_relay.env"
    if [ -f "$CONF_SRC" ]; then
        sudo cp "$CONF_SRC" /etc/hexapod-cam-relay/cam_relay.env
    else
        echo "CAM_UPSTREAM_URL=auto" | sudo tee /etc/hexapod-cam-relay/cam_relay.env >/dev/null
        echo "CAM_DEVICE_ID=hexapod-cam-01" | sudo tee -a /etc/hexapod-cam-relay/cam_relay.env >/dev/null
        echo "CAM_RELAY_HOST=127.0.0.1" | sudo tee -a /etc/hexapod-cam-relay/cam_relay.env >/dev/null
        echo "CAM_RELAY_PORT=8088" | sudo tee -a /etc/hexapod-cam-relay/cam_relay.env >/dev/null
        echo "CAM_MAX_CLIENTS=100" | sudo tee -a /etc/hexapod-cam-relay/cam_relay.env >/dev/null
    fi
fi

if [ ! -x "$APP/venv/bin/python" ]; then
    echo "==> Creating Python virtualenv"
    python3 -m venv "$APP/venv"
fi

sudo "$APP/venv/bin/pip" install --upgrade pip -q
sudo "$APP/venv/bin/pip" install -r "$APP/requirements.txt" -q

echo "==> Installing systemd unit"
sudo cp "$SRC/deploy/hexapod-cam-relay.service" /etc/systemd/system/hexapod-cam-relay.service
sudo chown -R spider:spider "$APP" /etc/hexapod-cam-relay
sudo systemctl daemon-reload
sudo systemctl enable hexapod-cam-relay
sudo systemctl restart hexapod-cam-relay

sleep 1
systemctl --no-pager -l status hexapod-cam-relay | head -10 || true
echo "==> Camera Relay service installation complete."