#!/usr/bin/env bash
# Install HueConnector into ./.venv and register the systemd service.
# Run from the project folder on the Jetson:  ./scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="${SUDO_USER:-$USER}"

sudo apt-get install -y python3-venv bluez
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "Created config.json - edit \"address\" before starting the service."
fi

sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" systemd/hueconnector.service \
  | sudo tee /etc/systemd/system/hueconnector.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable hueconnector
echo
echo "Installed. Next:"
echo "  1. Pair the bulb (see README) and put its MAC in config.json"
echo "  2. sudo systemctl start hueconnector"
echo "  3. journalctl -u hueconnector -f   # shows the HomeKit setup code"
