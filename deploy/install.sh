#!/usr/bin/env bash
# Install the AGV systemd services (agv-web, agv-drive) so the AGV starts at boot.
#
# Usage (on the Jetson):
#   sudo ./deploy/install.sh [--user NAME] [--dir /path/to/repo] [--dry-run]
#
# Defaults: user = the user who ran sudo, dir = the repo this script lives in.
# --dry-run renders the units to stdout and changes nothing (no sudo needed).
# The hotspot is separate: see deploy/setup_hotspot.sh.
# Roll back with: sudo ./deploy/uninstall.sh

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AGV_DIR="$(cd "$HERE/.." && pwd)"
AGV_USER="${SUDO_USER:-$(id -un)}"
DRY=0
UNITS=(agv-web agv-drive)

while [ $# -gt 0 ]; do
  case "$1" in
    --user) AGV_USER="${2:?--user needs a name}"; shift 2 ;;
    --dir) AGV_DIR="$(cd "${2:?--dir needs a path}" && pwd)"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

render() { sed -e "s|@AGV_USER@|$AGV_USER|g" -e "s|@AGV_DIR@|$AGV_DIR|g" "$HERE/$1.service"; }

case "$AGV_DIR$AGV_USER" in *[[:space:]]*) die "user and repo path must not contain whitespace (systemd unit paths): '$AGV_DIR'";; esac
[ -f "$AGV_DIR/web.py" ] && [ -f "$AGV_DIR/agv_drive_arduino.py" ] \
  || die "$AGV_DIR does not look like the AGV repo (web.py / agv_drive_arduino.py missing)."

if [ "$DRY" -eq 1 ]; then
  echo "# dry run: user=$AGV_USER dir=$AGV_DIR (nothing installed)"
  for u in "${UNITS[@]}"; do echo; echo "# ---- $u.service ----"; render "$u"; done
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "run with sudo."
command -v systemctl >/dev/null || die "systemctl not found."
id "$AGV_USER" >/dev/null 2>&1 || die "user '$AGV_USER' does not exist."
for g in dialout video; do
  getent group "$g" >/dev/null || die "group '$g' does not exist."
done

echo "Installing AGV services:"
echo "  user  = $AGV_USER   (units add groups dialout, video)"
echo "  repo  = $AGV_DIR"
echo "  units -> /etc/systemd/system/{agv-web,agv-drive}.service (overwritten if present)"
echo "  The AGV will start at every boot; motors stay OFF until START on the dashboard."

install -d -o "$AGV_USER" -g "$(id -gn "$AGV_USER")" "$AGV_DIR/logs"
for u in "${UNITS[@]}"; do
  render "$u" >"/etc/systemd/system/$u.service"
  chmod 644 "/etc/systemd/system/$u.service"
done
systemctl daemon-reload
systemctl enable --now agv-web.service agv-drive.service

cat <<MSG

Installed and started.

Manage:
  systemctl status agv-web agv-drive
  sudo systemctl restart agv-web agv-drive
  sudo systemctl disable --now agv-drive agv-web     # turn autostart off
Logs:
  tail -f $AGV_DIR/logs/web.log $AGV_DIR/logs/agv.log
  journalctl -u agv-web -u agv-drive -f
Roll back:
  sudo $AGV_DIR/deploy/uninstall.sh [--hotspot]

Jetson-side checks, in this order:
  1. iw list | grep -A8 'Supported interface modes'   # AP must be listed; if not, STOP, the hotspot cannot work
  2. nmcli device                                      # note the wifi device name for deploy/hotspot.env
  3. id -un; pwd                                       # confirm user and repo path
  4. python3 $AGV_DIR/tests/run_all.py                 # must print ALL PASSED
  5. systemd-analyze verify /etc/systemd/system/agv-web.service /etc/systemd/system/agv-drive.service
  6. cp $AGV_DIR/deploy/hotspot.env.example $AGV_DIR/deploy/hotspot.env && chmod 600 $AGV_DIR/deploy/hotspot.env   # edit the password
     sudo $AGV_DIR/deploy/setup_hotspot.sh            # use Ethernet/USB for SSH: this drops Wi-Fi SSH
  7. sudo reboot ; then on a phone join the SSID and open http://10.42.0.1:5000
  8. Unplug/replug the Arduino; watch: journalctl -u agv-drive -f
  9. While moving, take the phone out of range or turn its Wi-Fi off; the AGV must stop
MSG
