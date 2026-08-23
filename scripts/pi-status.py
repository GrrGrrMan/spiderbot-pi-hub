#!/usr/bin/env python3
# pi-hub/scripts/pi-status.py
import os
import socket
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(SCRIPT_DIR, "../conf")

def check_port(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except Exception:
        return False

def check_systemd(service_name):
    try:
        res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
        return res.stdout.strip() == "active"
    except Exception:
        return False

def main():
    print("=" * 60)
    print("           HEXAPOD PI-HUB DIAGNOSTIC STATUS")
    print("=" * 60)

    # 1. MQTT Broker
    tcp_mqtt = check_port("localhost", 1883)
    ws_mqtt = check_port("localhost", 9001)
    print(f"[*] Mosquitto TCP (1883)     : {'[✓] OK' if tcp_mqtt else '[!] DOWN'}")
    print(f"[*] Mosquitto WS  (9001)     : {'[✓] OK' if ws_mqtt else '[!] DOWN'}")

    # 2. Camera Relay
    cam_port = check_port("localhost", 8088)
    cam_active = check_systemd("hexapod-cam-relay")
    print(f"[*] Camera Relay  (8088)     : {'[✓] OK' if cam_port else '[!] DOWN'}")
    print(f"[*] Camera Relay Service     : {'[✓] ACTIVE' if cam_active else '[!] INACTIVE'}")

    # 3. OmniRoute Gateway
    omniroute_port = check_port("localhost", 20128)
    omniroute_active = check_systemd("omniroute") or check_systemd("omniroute.service")
    print(f"[*] OmniRoute Gateway (20128): {'[✓] OK' if omniroute_port else '[!] DOWN'}")
    print(f"[*] OmniRoute Service        : {'[✓] ACTIVE' if omniroute_active else '[?] DETACHED/RUNNING'}")

    # 4. NGINX & Web-UI
    http_ok = check_port("localhost", 80)
    print(f"[*] NGINX Gateway (80)       : {'[✓] OK' if http_ok else '[!] DOWN'}")

    # 5. AI Service
    ai_active = check_systemd("hexapod-ai")
    print(f"[*] Hexapod AI Service       : {'[✓] ACTIVE' if ai_active else '[!] INACTIVE'}")

    print("=" * 60)

if __name__ == "__main__":
    main()