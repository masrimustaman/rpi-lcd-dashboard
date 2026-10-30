#!/bin/bash
# Remove the status panel and its service.
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root:  sudo ./uninstall.sh"
    exit 1
fi

systemctl disable --now lcd-status.service 2>/dev/null || true
rm -f /etc/systemd/system/lcd-status.service
rm -f /usr/local/bin/lcd_status.py /usr/local/bin/lcd_touch.py /usr/local/bin/lcd-wifi
rm -f /etc/modprobe.d/blacklist-ads7846.conf
rm -rf /etc/lcd-status
# Drop the boot-time module load added by install.sh.
if [ -f /etc/modules ]; then
    sed -i '/^ads7846$/d' /etc/modules
fi
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# Measured on this panel: fbcon does not re-attach to the ILI9486 framebuffer by
# itself, so the display goes dark instead of showing a text console.
echo "removed.  Reboot to stop the panel.  Note it goes dark rather than showing"
echo "a text console - fbcon stays unbound on this display.  Re-run install.sh to"
echo "bring the dashboard back."
