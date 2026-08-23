# ==============================================================================
# 1. FRESH PI PROVISIONING (FOR NEW USERS / ZERO-TO-HERO SETUP)
# ==============================================================================

# A. Push entire workspace to a brand new Raspberry Pi
cd D:\Projects\hexapod-workspaces\V2\pi-hub
ssh spider@spider-w.local "mkdir -p ~/pi-hub"
scp -r * spider@spider-w.local:~/pi-hub/

# B. Run Turnkey Layer 1 -> Layer 6 Installers in sequence
ssh -t spider@spider-w.local "cd ~/pi-hub && \
  sudo bash scripts/01_setup_network.sh && \
  sudo bash scripts/02_setup_broker.sh && \
  sudo bash scripts/03_setup_gateway.sh && \
  sudo bash scripts/04_setup_omniroute.sh && \
  sudo bash services/cam-relay/deploy/install-cam-relay.sh && \
  sudo bash services/ai-service/deploy/install-ai-service.sh"

# C. Verify all 5 daemons are green
ssh spider@spider-w.local "python3 ~/pi-hub/scripts/pi-status.py"


# ==============================================================================
# 2. QUICK SERVICE DEPLOYS (INCREMENTAL UPDATES / EXISTING OMNIROUTE SETUP)
# ==============================================================================

# A. QUICK AI SERVICE UPDATE (Prompts, Actions, Animations, Pipeline, Providers)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp -r services/ai-service/* conf/ai.env spider@spider-w.local:/tmp/ai-service-staging/
ssh -t spider@spider-w.local "sudo cp -r /tmp/ai-service-staging/providers /tmp/ai-service-staging/*.py /tmp/ai-service-staging/*.json /opt/hexapod-ai/ && sudo cp /tmp/ai-service-staging/ai.env /etc/hexapod-ai/ai.env && sudo systemctl restart hexapod-ai && sudo journalctl -u hexapod-ai -f -n 25"

# B. QUICK OMNIROUTE CONFIG UPDATE (Gateway API keys, upstream models, port 20128)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp conf/omniroute.env spider@spider-w.local:/tmp/
ssh -t spider@spider-w.local "sudo cp /tmp/omniroute.env /etc/omniroute/omniroute.env && sudo systemctl restart omniroute && sudo journalctl -u omniroute -f -n 20"

# C. QUICK CAMERA RELAY UPDATE (1-to-N fanout, fast snapshot buffer, MQTT discovery)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp services/cam-relay/cam_relay.py conf/cam_relay.env spider@spider-w.local:/tmp/
ssh -t spider@spider-w.local "sudo cp /tmp/cam_relay.py /opt/hexapod-cam-relay/ && sudo cp /tmp/cam_relay.env /etc/hexapod-cam-relay/ && sudo systemctl restart hexapod-cam-relay && sudo journalctl -u hexapod-cam-relay -f -n 20"

# D. QUICK DIAGNOSTIC SCRIPT UPDATE
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp scripts/pi-status.py spider@spider-w.local:~/pi-hub/scripts/
ssh spider@spider-w.local "chmod +x ~/pi-hub/scripts/pi-status.py"

# E. QUICK NGINX GATEWAY UPDATE (Reverse proxy, CORS, snapshot route)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp conf/nginx-gateway.conf spider@spider-w.local:/tmp/
ssh -t spider@spider-w.local "sudo cp /tmp/nginx-gateway.conf /etc/nginx/sites-available/spiderbot && sudo nginx -t && sudo systemctl reload nginx"

# F. RESTART ALL ROBOT SERVICES AT ONCE (Full stack reload)
ssh -t spider@spider-w.local "sudo systemctl restart omniroute hexapod-ai hexapod-cam-relay nginx mosquitto"


# ==============================================================================
# 3. WEB UI FRONTEND DEPLOY
# ==============================================================================

cd D:\Projects\hexapod-workspaces\V2\web-ui
npm run build
scp -r build/* spider@spider-w.local:/home/spider/v2-web-ui/build/
ssh spider@spider-w.local "sudo chmod -R 755 /home/spider/v2-web-ui/build"


# ==============================================================================
# 4. LIVE MONITORING & LOGS
# ==============================================================================

# 1. System Health Dashboard (Mosquitto, Relay, OmniRoute:20128, Nginx, AI)
ssh spider@spider-w.local "python3 ~/pi-hub/scripts/pi-status.py"

# 2. Stream COMBINED Stack Logs (OmniRoute + AI Reasoning + Camera Relay)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-ai -u omniroute -u hexapod-cam-relay -f"

# 3. Stream ONLY AI Service Logs (STT, TTS, Kinematic JSON plans, Visual Tool triggers)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-ai -f -n 50"

# 4. Stream ONLY OmniRoute Gateway Logs (Upstream model routing, latency, token throughput)
ssh -t spider@spider-w.local "sudo journalctl -u omniroute -f -n 50"

# 5. Stream ONLY Camera Relay Logs (Viewers, upstream ESP32-CAM FPS)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-cam-relay -f -n 25"

# 6. Monitor ALL Live MQTT Traffic (Commands, Audio status, Directives, Telemetry)
ssh spider@spider-w.local "mosquitto_sub -t 'hexapod/#' -v"

# 7. Monitor ONLY AI Dialogues & Action Directives
ssh spider@spider-w.local "mosquitto_sub -t 'hexapod/+/ai/#' -v"


# ==============================================================================
# 5. DIRECT HTTP & OMNIROUTE GATEWAY CHECKS (Run from Windows PC)
# ==============================================================================

# 1. Verify OmniRoute Gateway is Active & List Upstream Models
curl -s http://spider-w.local:20128/v1/models

# 2. Smoke Test Primary Reasoning Dispatcher (DeepSeek via OmniRoute)
curl -s http://spider-w.local:20128/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer spiderbot" \
  -d "{\"model\":\"deepseek/deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"Respond strictly with: PONG\"}]}"

# 3. Check Live Camera Relay Health & Metrics
curl -s http://spider-w.local/cam-status

# 4. Fetch Single JPEG Snapshot directly from Ingress
curl -o snapshot.jpg http://spider-w.local/cam-snapshot


# ==============================================================================
# 6. REMOTE ROBOT & CAMERA MQTT CLI TRIGGERS
# ==============================================================================

# A. Trigger an AI Spoken & Motion Routine via MQTT
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-s3-01/ai' -m '{\"type\":\"text\",\"role\":\"user\",\"content\":\"Do 3 pushups and cheer\"}'"

# B. Trigger an Instant Deterministic Hardware Action
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-s3-01/ai' -m '{\"type\":\"text\",\"role\":\"user\",\"content\":\"walk forward\"}'"

# C. Turn On Flashlight (50% Brightness)
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"flash\":50}'"

# D. Turn Flashlight OFF
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"flash\":0}'"

# E. Switch Camera to High-Res FHD 1080p Inspection Mode
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"framesize\":\"FHD\",\"quality\":10}'"

# F. Reset Camera to Low-Latency VGA Stream (10 FPS)
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"framesize\":\"VGA\",\"quality\":12,\"fps\":10}'"