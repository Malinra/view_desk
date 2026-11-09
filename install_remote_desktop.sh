#!/usr/bin/env bash

set -euo pipefail

# ------------------------------------------------------------
# Simple installer for a web‑based per‑user remote desktop.
# Uses TigerVNC as the VNC server and noVNC as the HTML5 client.
# Authentication is performed by a small Flask app that checks
# the Linux account via PAM.
# ------------------------------------------------------------

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${CURRENT_DIR}/remote_desktop"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

echo "=== 安装配置信息 ==="
echo "目标路径: ${APP_DIR}"
echo "运行用户: ${CURRENT_USER}"
echo "==================="

# 1. Install required packages
sudo apt update
sudo apt install -y tigervnc-standalone-server tigervnc-common python3-pip python3-venv nginx

# 2. Install noVNC (web client)
NO_VNC_DIR="/opt/novnc"
if [ ! -d "$NO_VNC_DIR" ]; then
    sudo git clone https://github.com/novnc/noVNC.git "$NO_VNC_DIR"
    sudo git -C "$NO_VNC_DIR" checkout v1.5.0
    # noVNC ships with a tiny websockify implementation
    sudo git -C "$NO_VNC_DIR" clone https://github.com/novnc/websockify websockify
fi

# 3. Create a Python virtual environment for the Flask auth app
mkdir -p "$APP_DIR"
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
source "$APP_DIR/venv/bin/activate"
pip install -U pip
if [ -f "$APP_DIR/requirements.txt" ]; then
    pip install -r "$APP_DIR/requirements.txt"
else
    pip install Flask>=3.0.0 python-pam>=2.0.2 websockify>=0.11.0
fi
deactivate

# 4. Systemd service to run the Flask app
cat <<EOF | sudo tee /etc/systemd/system/remote-desktop-web.service
[Unit]
Description=Remote Desktop Web Auth Service
After=network.target

[Service]
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python app.py
Restart=on-failure
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now remote-desktop-web.service

# 5. NGINX reverse‑proxy configuration (HTTPS recommended)
sudo rm -f /etc/nginx/sites-enabled/default
cat <<'EOF' | sudo tee /etc/nginx/sites-available/remote-desktop.conf
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;  # replace with real cert
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;

    location / {
        proxy_pass http://127.0.0.1:5000;  # Flask app
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /novnc/ {
        alias /opt/novnc/;
        index index.html;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/remote-desktop.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx || echo "请确认 NGINX 与 SSL 证书配置"

echo "Installation complete. Open https://<your-host>/ or http://<your-host>:5000 in a browser."
