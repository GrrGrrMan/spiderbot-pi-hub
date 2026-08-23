#!/usr/bin/env bash
# pi-hub/scripts/03_setup_gateway.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[Layer 3] Configuring Nginx Ingress Gateway & Camera Stream Proxy..."

sudo apt-get install -y -qq nginx

# 1. Install Unified Nginx Configuration
sudo cp "${SCRIPT_DIR}/conf/nginx-gateway.conf" /etc/nginx/sites-available/spiderbot

# 2. Clean duplicate enabled sites and link only spiderbot
sudo rm -f /etc/nginx/sites-enabled/*
sudo ln -sf /etc/nginx/sites-available/spiderbot /etc/nginx/sites-enabled/spiderbot

# 3. Test & Reload Nginx
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
echo "[Layer 3] Nginx Ingress ready on Port 80 (Web-UI + /cam-stream)."