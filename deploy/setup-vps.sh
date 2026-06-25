#!/usr/bin/env bash
# One-time VPS setup for the Polybot watcher (Linux / systemd).
# Run from the polybot folder after creating .env:  bash deploy/setup-vps.sh
set -euo pipefail

cd "$(dirname "$0")/.."          # repo root (this script lives in deploy/)
DIR="$(pwd)"
USER_NAME="$(whoami)"

if [ ! -f .env ]; then
  echo "ERROR: no .env file. Create one first with your webhook:"
  echo "  echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...' > .env"
  exit 1
fi

echo "== Installing Python tooling =="
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

echo "== Building virtualenv + dependencies =="
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== Installing systemd service =="
sudo tee /etc/systemd/system/polybot.service >/dev/null <<EOF
[Unit]
Description=Polybot copy-trade / tracked-wallet watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/run.py watch
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now polybot

echo
echo "== Done. Polybot is running and will auto-start on boot. =="
echo "  status:   sudo systemctl status polybot"
echo "  live log: journalctl -u polybot -f"
echo "  restart:  sudo systemctl restart polybot   (after editing config or adding wallets)"
echo "  stop:     sudo systemctl stop polybot"
