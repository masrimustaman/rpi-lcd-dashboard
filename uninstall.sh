#!/bin/bash
# Remove the status panel and its service.
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root:  sudo ./uninstall.sh"
    exit 1
fi

systemctl disable --now lcd-status.service 2>/dev/null || true
rm -f /etc/systemd/system/lcd-status.service
rm -f /usr/local/bin/lcd_status.py /usr/local/bin/lcd-wifi
rm -f /etc/modprobe.d/blacklist-ads7846.conf
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "removed.  Reboot to get the normal text console back on the LCD."
