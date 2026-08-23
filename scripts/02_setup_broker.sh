#!/usr/bin/env bash
# pi-hub/scripts/02_setup_broker.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[Layer 2] Configuring Mosquitto MQTT & Avahi mDNS..."

sudo apt-get install -y -qq mosquitto mosquitto-clients avahi-daemon

# 1. Install Mosquitto Configuration
sudo cp "${SCRIPT_DIR}/conf/mosquitto.conf" /etc/mosquitto/conf.d/pi_hub.conf

# 2. Install Avahi mDNS Service
if [[ -f "${SCRIPT_DIR}/conf/mqtt.service" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/mqtt.service" /etc/avahi/services/mqtt.service
fi

sudo systemctl enable avahi-daemon mosquitto
sudo systemctl restart avahi-daemon mosquitto
echo "[Layer 2] MQTT Broker active on 1883 (TCP) and 9001 (WebSockets)."