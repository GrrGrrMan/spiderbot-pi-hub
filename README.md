# Hexapod V2 — Pi-Hub Gateway & Edge AI System

[![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20OS%20%2864--bit%29-red.svg)](https://www.raspberrypi.com/software/)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11-blue.svg)](https://www.python.org/)
[![MQTT](https://img.shields.io/badge/MQTT-Mosquitto%20v2.0-orange.svg)](https://mosquitto.org/)
[![Nginx](https://img.shields.io/badge/Nginx-Reverse%20Proxy%20%26%20Ingress-green.svg)](https://nginx.org/)
[![AI Engine](https://img.shields.io/badge/AI-OmniRoute%20%2B%20Faster--Whisper%20%2B%20Piper-purple.svg)](https://github.com/)

**Pi-Hub** is the central computing, networking, and cognitive intelligence gateway for the **Hexapod V2** robotics platform. Operating on a Raspberry Pi, it unifies physical hardware telemetry (ESP32-S3 kinematics and ESP32-CAM vision) with high-level multimodal AI capabilities, real-time audio/video relays, and a zero-trust network ingress for remote and offline operation.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [Key Features](#key-features)
- [Component Overview](#component-overview)
- [Prerequisites & Hardware Requirements](#prerequisites--hardware-requirements)
- [Step-by-Step Installation & Deployment](#step-by-step-installation--deployment)
- [Configuration Reference](#configuration-reference)
- [MQTT Communication Protocol](#mqtt-communication-protocol)
- [Skills & Cognitive Tool Registry](#skills--cognitive-tool-registry)
- [Diagnostics & Verification](#diagnostics--verification)
- [Extending & Developing Skills](#extending--developing-skills)

---

## System Architecture

The Pi-Hub acts as a multi-tier bridge orchestrating communication between the robot's microcontrollers, client applications, and cloud or local AI engines:

```
                                  ┌──────────────────────────────┐
                                  │   Browser / Mobile Web-UI    │
                                  │   (Tailscale / LAN / AP)     │
                                  └──────────────┬───────────────┘
                                                 │ HTTPS / WSS (:443 / :80)
┌────────────────────────────────────────────────▼─────────────────────────────────────────────────┐
│ RASPBERRY PI HUB                                                                                 │
│                                                                                                  │
│   ┌──────────────────────────────────────────────────────────────────────────────────────────┐   │
│   │ Nginx Ingress Gateway (:80, :443)                                                        │   │
│   │  ├── Web-UI Static Assets (/)                                                            │   │
│   │  ├── MQTT WebSockets Proxy (/mqtt -> :9001)                                              │   │
│   │  └── Camera MJPEG Proxy (/cam-stream, /cam-snapshot -> :8088)                            │   │
│   └────────────────────────────────────────┬─────────────────────────────────────────────────┘   │
│                                            │                                                     │
│   ┌────────────────────────────────────────▼─────────────────────────────────────────────────┐   │
│   │ Mosquitto MQTT Broker (:1883 TCP, :9001 WS)                                              │   │
│   └────┬───────────────────────────────────┬─────────────────────────────────────────────┬───┘   │
│        │                                   │                                             │       │
│   ┌────▼─────────────────────────┐    ┌────▼───────────────────────────┐    ┌────────────▼───┐   │
│   │ hexapod-ai Service           │    │ hexapod-cam-relay (:8088)      │    │ OmniRoute      │   │
│   │  ├── Pipeline & Agent        │    │  ├── MQTT Auto-Discovery       │    │ Gateway Proxy  │   │
│   │  ├── Memory & State Tracking │    │  ├── 1-to-N MJPEG Fanout       │    │ (:20128/v1)    │   │
│   │  ├── Faster-Whisper / Groq   │    │  └── Snapshot Buffer Cache     │    └────────────────┘   │
│   │  ├── Piper TTS (22kHz I2S)   │    └────────────────────────────────┘                         │
│   │  └── Native Skills Manager   │                                                               │
│   └──────────────────────────────┘                                                               │
└──────────────────────────┬───────────────────────────────────────────┬───────────────────────────┘
                           │ MQTT TCP (:1883)                          │ HTTP MJPEG Stream (:81)
┌──────────────────────────▼───────────────┐              ┌────────────▼──────────────┐
│ ESP32-S3 Kinematics Controller           │              │ ESP32-CAM Sensor Node     │
│  ├── 18-DOF Inverse Kinematics Engine    │              │  ├── OV2640 MJPEG Stream  │
│  ├── Dynamic Body Pose & Gaits           │              │  ├── Flashlight Control   │
│  └── I2S DAC Audio Subsystem             │              │  └── Auto-announcement IP │
└──────────────────────────────────────────┘              └───────────────────────────┘
```

---

## Key Features

- **Ingress Gateway (Nginx):** Single port `:80` / `:443` entry point serving the React UI, proxying WebSockets (`/mqtt`), and routing low-latency video feeds (`/cam-stream`).
- **Dynamic Networking:** Embedded Wi-Fi Hotspot (`spiderlink`) with automatic NAT masquerading, fallback offline self-signed SAN SSL, and automated Tailscale MagicDNS Let's Encrypt certificates.
- **Multimodal AI Pipeline (`hexapod-ai`):**
  - **Vision-Language Reasoning:** Continuous scene grounding via live camera frame snapshots.
  - **Speech-to-Text (STT):** Cloud Groq Whisper (`whisper-large-v3-turbo`) with automatic fallback to local CPU int8 `faster-whisper` (`tiny`).
  - **Text-to-Speech (TTS):** Low-latency sentence streaming with offline pre-warmed `piper-tts` (`en_US-lessac-medium`) transcoded into 22,050 Hz 16-bit Mono PCM binary chunks for the ESP32-S3 I2S amplifier.
- **Dynamic 1-to-N Camera Relay (`hexapod-cam-relay`):** Single-connection pull from the ESP32-CAM with fanout distribution across Web-UI clients and vision perception loops.
- **Embodied Kinematic Tasks:** Compiles dynamic 18-DOF joint keyframe trajectories, Cartesian body tilts, and gait sweeps directly from conversational language.
- **Smart Tool Ecosystem:** Live weather extraction, web searches (Wikipedia/DuckDuckGo), persistent countdown timers, and audio streaming (`yt-dlp` + `ffmpeg`) with automated ducking during speech.

---

## Component Overview

| Component | Path | Description |
|---|---|---|
| **Network Config** | `conf/hotspot.env`, `scripts/01_setup_network.sh` | AP hotspot (`192.168.4.1/24`), routing, and NAT masquerade. |
| **MQTT Broker** | `conf/mosquitto.conf`, `scripts/02_setup_broker.sh` | Mosquitto TCP (`1883`) and WebSocket (`9001`) broker + Avahi mDNS. |
| **Nginx Gateway** | `conf/nginx-gateway.conf`, `scripts/03_setup_gateway.sh` | Unified reverse proxy with Tailscale and offline SAN SSL support. |
| **OmniRoute Proxy** | `conf/omniroute.env`, `scripts/04_setup_omniroute.sh` | Local AI provider routing proxy on port `20128`. |
| **Camera Relay** | `services/cam-relay/cam_relay.py` | Asyncio HTTP video fanout proxy with MQTT IP auto-discovery. |
| **AI Perception Service**| `services/ai-service/ai_service.py` | Core cognitive daemon handling speech, memory, vision, and execution. |
| **Embodied Kinematics** | `services/ai-service/embodied_agent.py` | Decomposes compound language tasks into physical 18-DOF motions. |
| **Skills Engine** | `services/ai-service/skills/` | Modular smart skills (Media, Time, Weather, Web Search). |

---

## Prerequisites & Hardware Requirements

### Hardware
- **Compute:** Raspberry Pi 4 Model B or Raspberry Pi 5 (4GB+ RAM recommended for local STT/TTS).
- **Storage:** MicroSD Card (Class 10 / A2) or NVMe SSD with 16 GB+ free space.
- **Operating System:** Raspberry Pi OS (64-bit, Debian Bookworm or Bullseye).
- **Peripherals:** Wi-Fi interface (built-in or USB dongle), external speaker or ESP32-S3 I2S audio node.

### System Dependencies
Ensure system package indices are up to date and base utilities are present:
```bash
sudo apt-get update && sudo apt-get install -y \
    git curl wget python3 python3-pip python3-venv \
    ffmpeg mpv network-manager iptables-persistent avahi-daemon
```

---

## Step-by-Step Installation & Deployment

Deploying the Pi-Hub on a clean Raspberry Pi is automated via modular layered scripts.

### 1. Clone the Repository
```bash
cd /home/spider
git clone <repository-url> pi-hub
cd pi-hub
```

### 2. Configure Environment Files
Inspect and update the configuration files under `conf/`:
```bash
# Set your API keys and provider preferences
cp conf/ai.env.example conf/ai.env 2>/dev/null || true
nano conf/ai.env

# Configure Wi-Fi Hotspot credentials if needed
nano conf/hotspot.env
```

### 3. Run Layered System Setup

Execute the installation scripts sequentially:

```bash
# Layer 1: Network Gateway, Hotspot, and NAT Masquerading
bash scripts/01_setup_network.sh

# Layer 2: Mosquitto MQTT Broker (TCP 1883 & WS 9001) + Avahi mDNS
bash scripts/02_setup_broker.sh

# Layer 3: Nginx Gateway & Web-UI Ingress
bash scripts/03_setup_gateway.sh

# Layer 4: OmniRoute AI Gateway Service
bash scripts/04_setup_omniroute.sh

# Layer 5: Tailscale TLS Integration (Optional, for remote domains)
bash scripts/05_setup_tailscale_https.sh

# Layer 6: Offline 10-Year SAN SSL Certificate Generation
bash scripts/06_setup_offline_https.sh
```

### 4. Deploy Daemons & AI Models

Install the background systemd daemons and download required offline artifacts (`faster-whisper` and `piper` voice models):

```bash
# Install & Enable Camera Relay Service
bash services/cam-relay/deploy/install-cam-relay.sh

# Install & Enable Hexapod AI Service (downloads models and creates virtualenv)
bash services/ai-service/deploy/install-ai-service.sh
```

---

## Configuration Reference

Configuration files reside in `conf/` and are mirrored to `/etc/` during installation.

### `conf/ai.env`
```ini
DEVICE_ID="hexapod-s3-01"
CAM_DEVICE_ID="hexapod-cam-01"
LLM_ENABLED=1

# OmniRoute or OpenAI-Compatible API Endpoint
LLM_BASE_URL="http://127.0.0.1:20128/v1"
LLM_API_KEY="spiderbot"

# Target Models
LLM_MODEL="hexapod-vision"
LLM_VISION_MODEL="hexapod-vision"
SNAPSHOT_URL="http://127.0.0.1:8088/snapshot"

# Speech Configuration
TTS_MODEL="local"          # "local" (Piper 100% offline) or "hexapod-voice"
TTS_VOICE="alloy"
TTS_TIMEOUT_S=3.0
```

### `conf/cam_relay.env`
```ini
CAM_UPSTREAM_URL="auto"     # "auto" enables dynamic MQTT discovery of ESP32-CAM IP
CAM_DEVICE_ID="hexapod-cam-01"
CAM_RELAY_HOST="0.0.0.0"
CAM_RELAY_PORT=8088
CAM_MAX_CLIENTS=100
```

### `conf/hotspot.env`
```ini
HOTSPOT_SSID="spiderlink"
HOTSPOT_PASS="spiderbot"
HOTSPOT_IFACE="*"           # Auto-detects primary/secondary Wi-Fi adapter
```

---

## MQTT Communication Protocol

All inter-subsystem control occurs across Mosquitto over well-defined topics structured under `hexapod/{device_id}/`.

### Primary Topics

| Topic | Publisher | Description |
|---|---|---|
| `hexapod/{device_id}/cmd` | `ai-service` / Web-UI | Real-time motion, pose, joint angle, and power commands to ESP32-S3. |
| `hexapod/{device_id}/telemetry` | ESP32-S3 | Battery voltage, operational state, IMU orientation, and temperatures. |
| `hexapod/{device_id}/audio` | `ai-service` / Web-UI | Raw 10-byte header binary frames (I2S PCM audio stream). |
| `hexapod/{device_id}/audio/status` | ESP32-S3 | Audio buffer playback state (`{"state": "playing" \| "idle"}`). |
| `hexapod/{device_id}/ai` | `ai-service` / Web-UI | Conversational chat events, STT transcriptions, and agent thoughts. |
| `hexapod/{device_id}/ai/status` | `ai-service` | Heartbeat containing active memory, LLM config, and skill states. |
| `hexapod/{device_id}/ai/memory/cmd` | Web-UI | Long-term memory modifications (`set_fact`, `clear_session`, etc.). |
| `hexapod/{cam_device_id}/telemetry`| ESP32-CAM | Camera IP announcement, Wi-Fi RSSI, and stream metadata. |
| `hexapod/{cam_device_id}/cmd` | `ai-service` / Web-UI | LED flashlight intensity, FPS, exposure, and JPEG quality commands. |

### Kinematic Motion Payload Example (`/cmd`)
```json
{
  "type": "motion",
  "gait": "tripod",
  "vx": 40.0,
  "vy": 0.0,
  "omega": 0.0,
  "step_height": 38.0,
  "cycle_time": 0.8,
  "hip_stance": 20.0,
  "leg_stance": 0.0,
  "pos_z": 0.0,
  "roll": 0.0,
  "pitch": 0.0,
  "yaw": 0.0,
  "duration_ms": 2500,
  "lease_ms": 350
}
```

### Dynamic Joint Control Payload Example (`/cmd`)
```json
{
  "type": "sequence",
  "name": "dynamic_joint_motion",
  "duration_ms": 2500,
  "keyframes": [
    {
      "duration_ms": 800,
      "easing": "easeInOutCubic",
      "joints": {
        "rf": { "alpha": 20.0, "beta": 55.0, "gamma": -45.0 },
        "lf": { "alpha": -20.0, "beta": 55.0, "gamma": -45.0 }
      }
    }
  ]
}
```

### Binary Audio Frame Specification (`/audio`)
Binary frames transmitted over MQTT feature a 10-byte little-endian header followed by raw 16-bit 22,050 Hz Mono PCM samples:
```
Offset | Type   | Description
-------|--------|------------------------------------------------
0x00   | uint8  | Magic identifier (0xAA)
0x01   | uint8  | Action flag (0x00 = Stream Playback)
0x02   | uint32 | Flow ID (Random session identifier)
0x06   | uint16 | Sequence index (0 .. Total-1)
0x08   | uint16 | Total chunks in stream (0 for continuous media)
0x0A+  | bytes  | Raw PCM audio chunk (up to 4096 bytes)
```

---

## Skills & Cognitive Tool Registry

The `SkillManager` registers tools that are exposed directly to the LLM via OpenAI-compatible function calling schemas:

| Tool Name | Parameters | Description |
|---|---|---|
| `inspect_scene` | `query: str` | Captures a live frame from `/snapshot` and grounds multimodal reasoning. |
| `get_weather` | `location: str` | Returns temperature, humidity, wind speed, and WMO conditions via Open-Meteo. |
| `web_search` | `query: str` | Performs instant web summaries via DuckDuckGo and Wikipedia APIs. |
| `set_timer` | `duration_seconds: int`, `label: str` | Sets an asynchronous timer that triggers audio and voice alarms upon expiration. |
| `cancel_timer` | `label: str` | Cancels active countdown timers. |
| `play_music` | `query: str` | Resolves YouTube / sound queries via `yt-dlp`, transcoding via `ffmpeg` to I2S. |
| `pause_music` | *None* | Pauses active media stream. |
| `resume_music`| *None* | Resumes active media stream. |
| `stop_music`  | *None* | Terminates media worker process. |

---

## Diagnostics & Verification

### 1. Hub Diagnostics Script
Run the built-in system checker to verify all ports, sockets, and services:
```bash
python3 scripts/pi-status.py
```
*Expected Output:*
```text
============================================================
           HEXAPOD PI-HUB DIAGNOSTIC STATUS
============================================================
[*] Mosquitto TCP (1883)     : [✓] OK
[*] Mosquitto WS  (9001)     : [✓] OK
[*] Camera Relay  (8088)     : [✓] OK
[*] Camera Relay Service     : [✓] ACTIVE
[*] OmniRoute Gateway (20128): [✓] OK
[*] OmniRoute Service        : [✓] ACTIVE
[*] NGINX Gateway (80)       : [✓] OK
[*] Hexapod AI Service       : [✓] ACTIVE
============================================================
```

### 2. Service Regression Self-Test
Verify action parser expansions, audio sentence chunkers, and prompt sanitation logic:
```bash
/opt/hexapod-ai/venv/bin/python services/ai-service/selftest.py
```

### 3. Inspect System Logs
```bash
# Hexapod AI Cognitive Service
sudo journalctl -u hexapod-ai -f -n 50

# Camera Fanout Relay
sudo journalctl -u hexapod-cam-relay -f -n 50

# OmniRoute Proxy
sudo journalctl -u omniroute -f -n 50
```

---

## Extending & Developing Skills

### Adding a New Skill
1. **Implement Skill Logic:** Create a module in `services/ai-service/skills/` (e.g., `system_info_skill.py`).
2. **Register Tool Schema:** Add the tool definition to `SKILL_TOOLS` in `services/ai-service/providers/llm.py`.
3. **Dispatch Tool Call:** Add execution handlers in `services/ai-service/skills/skill_manager.py`.

### Adding New Physical Animations
1. Open `services/ai-service/animations.json`.
2. Define a new animation object with keyframe timestamps (`t_pct`), Cartesian body positions (`tx`, `ty`, `tz`, `rx`, `ry`, `rz`), or individual joint overrides (`joints`).
3. Re-run `selftest.py` to confirm keyframe interpolation matches duration bounds.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.