# QUICK AI UPDATE 
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp -r services/ai-service/* spider@spider-w.local:/tmp/ai-service/
ssh -t spider@spider-w.local "sudo cp -r /tmp/ai-service/* /opt/hexapod-ai/ && sudo systemctl restart hexapod-ai && sudo journalctl -u hexapod-ai -f -n 40"

# FULL PI-HUB STACK UPDATE
cd D:\Projects\hexapod-workspaces\V2\pi-hub
scp -r * spider@spider-w.local:/home/spider/pi-hub/
ssh -t spider@spider-w.local "cd ~/pi-hub && find . -type f -exec sed -i 's/\r$//' {} + && chmod +x setup.sh scripts/*.sh 2>/dev/null && sudo bash ./setup.sh all"


# WEB UI FRONTEND DEPLOY
cd D:\Projects\hexapod-workspaces\V2\web-ui
npm run build
scp -r build/* spider@spider-w.local:/home/spider/v2-web-ui/build/


# LIVE MONITORING & LOGS
# 1. Health Status Dashboard
ssh spider@spider-w.local "python3 ~/pi-hub/scripts/pi-status.py"

# 2. Stream Live AI Service Logs (Whisper, Piper, LLM decisions)
ssh -t spider@spider-w.local "sudo journalctl -u hexapod-ai -f"

# 3. Monitor All Live MQTT Traffic Across the Robot
ssh spider@spider-w.local "mosquitto_sub -t 'hexapod/#' -v"

# 4. Stream Nginx Ingress & Camera Proxy Logs
ssh -t spider@spider-w.local "sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log"