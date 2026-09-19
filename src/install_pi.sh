#!/usr/bin/env bash
# install_pi.sh - Raspberry Pi 4 + display: Matter server, logger, tools and fullscreen dashboard.
# Needs: Raspberry Pi OS (64-bit) WITH desktop. Put all project files in one folder, then:
#   sudo bash install_pi.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash install_pi.sh"; exit 1; }
# 32-bit Pi OS can still report an aarch64 kernel, so check the userland architecture
[ "$(dpkg --print-architecture)" = "arm64" ] || {
  echo "Needs 64-bit Raspberry Pi OS (matter server image is arm64/amd64 only)"; exit 1; }
for f in install.sh aqara_logger.py dashboard.py note.py export.py; do
  [ -f "$HERE/$f" ] || { echo "$f not found next to install_pi.sh"; exit 1; }
done

bash "$HERE/install.sh"            # Docker, matter server, logger, aqara-* commands

apt-get install -y python3-tk fonts-dejavu-core
install -m 755 "$HERE/dashboard.py" /opt/aqara/dashboard.py

# XDG autostart: works for the X11, wayfire and labwc desktops of Raspberry Pi OS.
# --corrections: tiles use corrections.json (empty until the first calibration).
mkdir -p /etc/xdg/autostart
cat > /etc/xdg/autostart/aqara-dashboard.desktop <<'DESK'
[Desktop Entry]
Type=Application
Name=Aqara Dashboard
Exec=python3 /opt/aqara/dashboard.py /opt/aqara/app/latest.json --corrections
DESK

# boot straight into the desktop, logged in, so the dashboard comes back after a power cut
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_boot_behaviour B4
else
  echo "raspi-config not found: set desktop autologin yourself"
fi

echo
echo "Dashboard starts after: sudo reboot"
echo "Optional, for an always-on screen: sudo raspi-config -> Display Options -> Screen Blanking -> No"
