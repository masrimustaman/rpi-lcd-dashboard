#!/bin/bash
# Install the 480x320 status panel on Raspberry Pi OS (Bookworm/Trixie).
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root:  sudo ./install.sh"
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== packages =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-pil python3-numpy fonts-dejavu-core libraspberrypi-bin

echo "== files =="
install -m 755 "$HERE/lcd_status.py"        /usr/local/bin/lcd_status.py
install -m 755 "$HERE/lcd-wifi"             /usr/local/bin/lcd-wifi
install -m 644 "$HERE/lcd-status.service"   /etc/systemd/system/lcd-status.service

echo "== disable touch controller (ads7846) =="
# This build is display-only.  Remove this file if you have a working
# touch panel and want the driver back.
echo "blacklist ads7846" > /etc/modprobe.d/blacklist-ads7846.conf

echo "== service =="
systemctl daemon-reload
systemctl enable lcd-status.service
systemctl restart lcd-status.service
sleep 4
systemctl is-active lcd-status.service

cat <<'EOF'

Done.  The panel is now showing the dashboard.

If the touch driver is still loaded in this session, reboot (or run
`modprobe -r ads7846`) so the blacklist takes effect:

    sudo reboot

Uninstall with:  sudo ./uninstall.sh
EOF
