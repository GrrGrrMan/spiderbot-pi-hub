#!/usr/bin/env python3
"""
Pi-Hub Health & Diagnostic Utility
Run on the Raspberry Pi: python3 scripts/pi-status.py
"""

import os
import socket
import subprocess
import sys


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
    print("=" * 55)
    print("        HEXAPOD PI-HUB DIAGNOSTIC STATUS")
    print("=" * 55)

    # 1. Mosquitto MQTT Broker
    tcp_mqtt = check_port("localhost", 1883)
    ws_mqtt = check_port("localhost", 9001)
    print(f"[*] Mosquitto TCP Port 1883  : {'[✓] OK' if tcp_mqtt else '[!] DOWN'}")
    print(f"[*] Mosquitto WS  Port 9001  : {'[✓] OK' if ws_mqtt else '[!] DOWN'}")

    # 2. NGINX Web Server
    http_80 = check_port("localhost", 80)
    web_dir = os.path.exists("/home/spider/v2-web-ui/build/index.html")
    print(f"[*] NGINX HTTP Port 80       : {'[✓] OK' if http_80 else '[!] DOWN'}")
    print(f"[*] Web-UI Build Present     : {'[✓] OK' if web_dir else '[!] MISSING (/home/spider/v2-web-ui/build)'}")

    # 3. System Services
    ai_active = check_systemd("hexapod-ai")
    avahi_active = check_systemd("avahi-daemon")
    print(f"[*] Avahi mDNS Service       : {'[✓] ACTIVE' if avahi_active else '[!] INACTIVE'}")
    print(f"[*] AI Service (hexapod-ai)  : {'[✓] ACTIVE' if ai_active else '[!] INACTIVE'}")

    # 4. AI Key & Models
    key_exists = os.path.exists("/etc/hexapod-ai/groq.key") or bool(os.environ.get("GROQ_API_KEY"))
    whisper_exists = os.path.exists("/opt/hexapod-ai/models")
    voice_exists = os.path.exists("/opt/hexapod-ai/voices/en_US-lessac-medium.onnx")
    print(f"[*] Groq API Key Configured  : {'[✓] YES' if key_exists else '[!] NO (/etc/hexapod-ai/groq.key)'}")
    print(f"[*] Piper Voice Asset Present: {'[✓] YES' if voice_exists else '[!] NO'}")

    print("=" * 55)


if __name__ == "__main__":
    main()