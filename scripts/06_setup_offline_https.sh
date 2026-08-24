#!/usr/bin/env bash
# pi-hub/scripts/06_setup_offline_https.sh
set -euo pipefail

echo "[Layer 6] Generating 10-Year Local SAN SSL Certificate for Hotspot & LAN..."

sudo mkdir -p /etc/ssl/spiderbot

# Generate a certificate covering 192.168.4.1, spider-w.local, and localhost
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/ssl/spiderbot/selfsigned.key \
    -out /etc/ssl/spiderbot/selfsigned.crt \
    -subj "/CN=spider-w.local" \
    -addext "subjectAltName=DNS:spider-w.local,DNS:spiderbot.local,DNS:localhost,IP:192.168.4.1,IP:127.0.0.1"

sudo chmod 600 /etc/ssl/spiderbot/selfsigned.key
sudo chmod 644 /etc/ssl/spiderbot/selfsigned.crt

echo "   -> Certificate ready at /etc/ssl/spiderbot/"

# Re-apply Nginx config and reload
sudo cp /home/spider/pi-hub/conf/nginx-gateway.conf /etc/nginx/sites-available/spiderbot
sudo nginx -t
sudo systemctl reload nginx

echo "[Layer 6] Offline HTTPS active on https://192.168.4.1 and https://spider-w.local"