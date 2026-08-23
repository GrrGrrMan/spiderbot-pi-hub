# ==============================================================================
# 1. DIRECT SERVICE DEPLOYS (Native scp + ssh)
# ==============================================================================

# A. QUICK AI SERVICE UPDATE (Prompts, actions, animations)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp -r services/ai-service/* spider@spider-w.local:/tmp/ai-service/
ssh -t spider@spider-w.local "sudo cp -r /tmp/ai-service/* /opt/hexapod-ai/ && sudo systemctl restart hexapod-ai && sudo journalctl -u hexapod-ai -f -n 25"

# B. QUICK CAMERA RELAY UPDATE (1-to-N fanout & MQTT discovery)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp services/cam-relay/cam_relay.py conf/cam_relay.env spider@spider-w.local:/tmp/
ssh -t spider@spider-w.local "sudo cp /tmp/cam_relay.py /opt/hexapod-cam-relay/ && sudo cp /tmp/cam_relay.env /etc/hexapod-cam-relay/ && sudo systemctl restart hexapod-cam-relay && sudo journalctl -u hexapod-cam-relay -f -n 20"

# C. QUICK NGINX GATEWAY UPDATE (Reverse proxy & CORS)
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp conf/nginx-gateway.conf spider@spider-w.local:/tmp/
ssh -t spider@spider-w.local "sudo cp /tmp/nginx-gateway.conf /etc/nginx/sites-available/spiderbot && sudo nginx -t && sudo systemctl reload nginx"

# D. RESTART ALL ROBOT SERVICES AT ONCE
ssh -t spider@spider-w.local "sudo systemctl restart hexapod-ai hexapod-cam-relay nginx mosquitto"


# ==============================================================================
# 2. WEB UI FRONTEND DEPLOY
# ==============================================================================

cd D:\Projects\hexapod-workspaces\V2\web-ui
npm run build
scp -r build/* spider@spider-w.local:/home/spider/v2-web-ui/build/
ssh spider@spider-w.local "sudo chmod -R 755 /home/spider/v2-web-ui/build"


# ==============================================================================
# 3. LIVE MONITORING & LOGS
# ==============================================================================

# 1. System Health Dashboard
ssh spider@spider-w.local "python3 ~/pi-hub/scripts/pi-status.py"

# 2. Stream COMBINED Robot Logs (AI Voice + Camera Relay side-by-side)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-ai -u hexapod-cam-relay -f"

# 3. Stream ONLY Camera Relay Logs (View active viewers & upstream FPS)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-cam-relay -f"

# 4. Stream ONLY AI Service Logs (STT, TTS, LLM decisions)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-ai -f"

# 5. Monitor ALL Live MQTT Traffic
ssh spider@spider-w.local "mosquitto_sub -t 'hexapod/#' -v"

# 6. Monitor Camera & Robot Telemetry
ssh spider@spider-w.local "mosquitto_sub -t 'hexapod/+/telemetry' -v"


# ==============================================================================
# 4. DIRECT HTTP ENDPOINT CHECKS (Run directly from your PC)
# ==============================================================================

# 1. Check Live Camera Relay Stats (Viewers, FPS, auto-discovered IP)
curl -s http://spider-w.local/cam-status

# 2. Verify Stream Headers (Single CORS origin check)
curl -I http://spider-w.local/cam-stream

# 3. Fetch Single High-Res Snapshot Frame
curl -o snapshot.jpg http://spider-w.local/cam-snapshot


# ==============================================================================
# 5. REMOTE CAMERA MQTT CONTROLS (CLI Triggers)
# ==============================================================================

# Turn On Flashlight (50% Power)
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"flash\":50}'"

# Turn Flashlight OFF
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"flash\":0}'"

# Switch OV3660 to High-Res FHD 1080p Inspection Mode
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"framesize\":\"FHD\",\"quality\":10}'"

# Reset to Low-Latency VGA Stream (10 FPS)
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"framesize\":\"VGA\",\"quality\":12,\"fps\":10}'"

# Toggle Orientation (Vertical Flip & Mirror)
ssh spider@spider-w.local "mosquitto_pub -t 'hexapod/hexapod-cam-01/cmd' -m '{\"type\":\"camera\",\"vflip\":true,\"hmirror\":true}'"