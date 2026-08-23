#!/usr/bin/env bash
# pi-hub/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 [all | network | broker | gateway | ai | status]"
    echo ""
    echo "Modular Commands:"
    echo "  all       Run complete end-to-end setup (Layers 1-4)"
    echo "  network   Setup AP Hotspot, IP Forwarding & NAT (Layer 1)"
    echo "  broker    Setup Mosquitto MQTT & Avahi mDNS (Layer 2)"
    echo "  gateway   Setup Nginx Web & Camera Stream Proxy (Layer 3)"
    echo "  ai        Setup AI Service, Piper TTS & Whisper STT (Layer 4)"
    echo "  status    Check system health and service status (Layer 5)"
    exit 1
}

CMD="${1:-all}"

case "$CMD" in
    network)
        bash "${SCRIPT_DIR}/scripts/01_setup_network.sh"
        ;;
    broker)
        bash "${SCRIPT_DIR}/scripts/02_setup_broker.sh"
        ;;
    gateway)
        bash "${SCRIPT_DIR}/scripts/03_setup_gateway.sh"
        ;;
    ai)
        sudo bash "${SCRIPT_DIR}/services/ai-service/deploy/install-ai-service.sh"
        ;;
    status)
        python3 "${SCRIPT_DIR}/scripts/pi-status.py"
        ;;
    all)
        echo "=========================================="
        echo "   Deploying Hexapod Pi-Hub Full Stack    "
        echo "=========================================="
        bash "${SCRIPT_DIR}/scripts/01_setup_network.sh"
        bash "${SCRIPT_DIR}/scripts/02_setup_broker.sh"
        bash "${SCRIPT_DIR}/scripts/03_setup_gateway.sh"
        sudo bash "${SCRIPT_DIR}/services/ai-service/deploy/install-ai-service.sh"
        echo ""
        python3 "${SCRIPT_DIR}/scripts/pi-status.py"
        ;;
    *)
        usage
        ;;
esac