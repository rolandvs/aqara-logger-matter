#!/usr/bin/env bash
# install.sh - turn a fresh Debian 12/13, Ubuntu 24.04 or Raspberry Pi OS machine into an
# Aqara T1 logger (no display). For a Pi with a screen, run install_pi.sh instead: it calls this.
# Put the project files next to this file, then:  sudo bash install.sh
set -euo pipefail

BASE=/opt/aqara
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash install.sh"; exit 1; }
[ -f "$HERE/aqara_logger.py" ] || { echo "aqara_logger.py not found next to install.sh"; exit 1; }

# Matter needs IPv6 (link-local multicast + mDNS)
if [ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null || echo 1)" != "0" ]; then
  echo "WARNING: IPv6 is disabled. Matter will not work until it is enabled."
fi

apt-get update
apt-get install -y curl ca-certificates
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

mkdir -p "$BASE/data" "$BASE/app"
install -m 644 "$HERE/aqara_logger.py" "$BASE/app/aqara_logger.py"
chown -R 1000:1000 "$BASE/data"   # matter server container runs as UID 1000

cat > "$BASE/compose.yaml" <<'EOF'
services:
  matter-server:
    image: ghcr.io/matter-js/matterjs-server:stable
    network_mode: host
    restart: unless-stopped
    volumes:
      - /opt/aqara/data:/data
  logger:
    image: python:3.12-slim
    network_mode: host
    restart: unless-stopped
    depends_on: [matter-server]
    volumes:
      - /opt/aqara/app:/app
    command: sh -c "pip install -q websockets && python /app/aqara_logger.py --url ws://localhost:5580/ws --out /app/sensor_log.csv --latest /app/latest.json --names /app/names.json"
EOF

# helper: aqara-commission <pairing-code>
cat > /usr/local/bin/aqara-commission <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ $# -eq 1 ] || { echo "Usage: aqara-commission <pairing-code>"; exit 1; }
docker compose -f /opt/aqara/compose.yaml run --rm logger \
  sh -c "pip install -q websockets && python /app/aqara_logger.py --url ws://localhost:5580/ws --commission '$1'"
docker compose -f /opt/aqara/compose.yaml restart logger
EOF
chmod +x /usr/local/bin/aqara-commission

# helper: aqara-logger --watch | --dump   (diagnostics, next to the running logger)
cat > /usr/local/bin/aqara-logger <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --watch|--dump) ;;
  *) echo "Usage: aqara-logger --watch   (every report, live)"
     echo "       aqara-logger --dump    (everything the hub exposes per sensor)"; exit 1 ;;
esac
exec docker compose -f /opt/aqara/compose.yaml run --rm logger sh -c \
  'pip install -q websockets && exec python /app/aqara_logger.py --url ws://localhost:5580/ws --names /app/names.json "$@"' \
  sh "$@"
EOF
chmod +x /usr/local/bin/aqara-logger

# optional tools: remarks (aqara-note) and chart exports (aqara-export), host Python only
for tool in note export; do
  [ -f "$HERE/$tool.py" ] || continue
  install -m 755 "$HERE/$tool.py" "$BASE/app/$tool.py"
  printf '#!/usr/bin/env bash\nexec python3 %s/app/%s.py "$@"\n' "$BASE" "$tool" \
    > "/usr/local/bin/aqara-$tool"
  chmod +x "/usr/local/bin/aqara-$tool"
done
# let the normal user add remarks and calibrate without sudo
for f in remarks.csv corrections.json; do
  touch "$BASE/app/$f"
  chown "${SUDO_USER:-root}" "$BASE/app/$f"
done

docker compose -f "$BASE/compose.yaml" up -d

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done."
echo "  Matter UI : http://$IP:5580   (Matter server: paired devices)"
echo "  Pair M100 : sudo aqara-commission <pairing-code>"
echo "  Live log  : sudo aqara-logger --watch"
echo "  CSV       : $BASE/app/sensor_log.csv"
[ -x /usr/local/bin/aqara-note ] && echo "  Remark    : aqara-note \"moved 3 to the bedroom\"   (aqara-note -h)"
[ -x /usr/local/bin/aqara-export ] && echo "  Chart CSV : aqara-export   /   aqara-export overlay   (aqara-export -h)"
true
