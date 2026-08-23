#!/usr/bin/env python3
"""
Pi-Hub Health & Diagnostic Utility
Dynamically inspects services using conf/ environment files as the single source of truth.
"""

import os
import socket
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(SCRIPT_DIR, "../conf")

def get_env_var(file_path, key, default):
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    return default

# Single Points of Truth:
CAM_ENV_FILE = "/etc/hexapod-cam-relay/cam_relay.env"
if not os.path.exists(CAM_ENV_FILE):
    CAM_ENV_FILE = os.path.join(CONF_DIR, "cam_relay.env")

RELAY_PORT = int(get_env_var(CAM_ENV_FILE, "CAM_RELAY_PORT", 8088))
MQTT_PORT = 1883
WS_PORT = 9001
HTTP_PORT = 80


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
    print("=" * 58)
    print("           HEXAPOD PI-HUB DIAGNOSTIC STATUS")
    print("=" * 58)

    # 1. MQTT Broker (TCP & WebSockets)
    tcp_mqtt = check_port("localhost", MQTT_PORT)
    ws_mqtt = check_port("localhost", WS_PORT)
    print(f"[*] Mosquitto TCP Port {MQTT_PORT:<5}  : {'[✓] OK' if tcp_mqtt else '[!] DOWN'}")
    print(f"[*] Mosquitto WS  Port {WS_PORT:<5}  : {'[✓] OK' if ws_mqtt else '[!] DOWN'}")

    # 2. Camera Relay (Port loaded dynamically from cam_relay.env)
    cam_relay_active = check_systemd("hexapod-cam-relay")
    cam_port = check_port("localhost", RELAY_PORT)
    print(f"[*] Camera Relay Port {RELAY_PORT:<5} : {'[✓] OK' if cam_port else '[!] DOWN'}")
    print(f"[*] Camera Relay Service    : {'[✓] ACTIVE' if cam_relay_active else '[!] INACTIVE'}")

    # 3. NGINX Gateway
    http_ok = check_port("localhost", HTTP_PORT)
    web_dir = os.path.exists("/home/spider/v2-web-ui/build/index.html")
    print(f"[*] NGINX HTTP Port {HTTP_PORT:<5}   : {'[✓] OK' if http_ok else '[!] DOWN'}")
    print(f"[*] Web-UI Build Present    : {'[✓] OK' if web_dir else '[!] MISSING'}")

    # 4. AI and Discovery Services
    ai_active = check_systemd("hexapod-ai")
    avahi_active = check_systemd("avahi-daemon")
    print(f"[*] Avahi mDNS Service      : {'[✓] ACTIVE' if avahi_active else '[!] INACTIVE'}")
    print(f"[*] AI Service (hexapod-ai) : {'[✓] ACTIVE' if ai_active else '[!] INACTIVE'}")

    print("=" * 58)


if __name__ == "__main__":
    main()