#!/usr/bin/env bash
# Script to install Avahi MQTT service discovery on the Raspberry Pi
# Broadcasts _mqtt._tcp (port 1883) so hexapod clients can auto-discover the broker via mDNS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Initializing Avahi MQTT DNS-SD installer..."

# 1. Ensure Avahi daemon is installed on the Pi
if ! command -v avahi-daemon &> /dev/null; then
    echo "Installing avahi-daemon..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq avahi-daemon
fi

# 2. Copy the service definition file to the system config folder
echo "Copying mqtt.service to /etc/avahi/services/..."
if [[ -f "${SCRIPT_DIR}/conf/mqtt.service" ]]; then
    sudo cp "${SCRIPT_DIR}/conf/mqtt.service" /etc/avahi/services/mqtt.service
    echo "   -> Installed /etc/avahi/services/mqtt.service"
else
    echo "   -> ERROR: conf/mqtt.service not found!"
    exit 1
fi

# 3. Reload Avahi to start broadcasting
echo "Restarting avahi-daemon..."
sudo systemctl restart avahi-daemon
sudo systemctl enable avahi-daemon

echo "Success! The Raspberry Pi is now broadcasting its MQTT Broker service on _mqtt._tcp (port 1883)."