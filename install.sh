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
    python3-pil python3-numpy fonts-dejavu-core

# vcgencmd lives in libraspberrypi-bin on Bookworm and raspi-utils on Trixie.
if ! command -v vcgencmd >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libraspberrypi-bin >/dev/null 2>&1 \
      || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq raspi-utils >/dev/null 2>&1 \
      || echo "   note: vcgencmd unavailable, the throttle card will show '?'"
fi

echo "== files =="
install -m 755 "$HERE/lcd_status.py"        /usr/local/bin/lcd_status.py
install -m 755 "$HERE/lcd_touch.py"         /usr/local/bin/lcd_touch.py
install -m 755 "$HERE/lcd-wifi"             /usr/local/bin/lcd-wifi
install -m 644 "$HERE/lcd-status.service"   /etc/systemd/system/lcd-status.service

echo "== enable touch controller (ads7846) =="
# The touch controller shares the panel's SPI bus on spi0.1.  Earlier revisions
# of this project blacklisted the driver and ran display-only; clear any stale
# blacklist so the buttons work, then make sure it loads at boot.
rm -f /etc/modprobe.d/blacklist-ads7846.conf
if [ -f /etc/modules ] && ! grep -qx ads7846 /etc/modules; then
    echo ads7846 >> /etc/modules
fi

# dtoverlay=nospi10 turns spi0.1 off, which silently kills touch while the
# display keeps working, so check for it rather than letting it confuse people.
if grep -qE '^[[:space:]]*dtoverlay=nospi10' /boot/firmware/config.txt 2>/dev/null; then
    echo "   WARNING: dtoverlay=nospi10 is set in /boot/firmware/config.txt."
    echo "            That disables spi0.1, which the touch controller needs."
    echo "            Comment that line out and reboot, or touch will not work."
fi

echo "== service =="
systemctl daemon-reload
systemctl enable lcd-status.service
systemctl restart lcd-status.service
sleep 4
systemctl is-active lcd-status.service

cat <<'EOF'

Done.  The panel is now showing the dashboard.

Touch is enabled, but needs a one-time calibration.  After the reboot, the
panel shows four crosshairs in turn - tap each one squarely with a stylus or
thumbnail.  The fit is checked before it is accepted (the taps must span both
raw axes, no coefficient may exceed 1.0, and the residual must stay under
25 px); a rejected fit is reported in the log and the wizard simply repeats.

    sed -n '/calibration/p' <(journalctl -u lcd-status -b)

The result is stored in /etc/lcd-status/touch.json and survives reboots.  To
start over, delete that file and restart the service:

    sudo rm /etc/lcd-status/touch.json && sudo systemctl restart lcd-status

Uninstall with:  sudo ./uninstall.sh
EOF
