#!/usr/bin/env bash
# Roll back deploy/install.sh (and optionally deploy/setup_hotspot.sh).
#
# Usage: sudo ./deploy/uninstall.sh [--hotspot]
#   (default)   stop and disable agv-drive / agv-web, remove their unit files.
#   --hotspot   also delete the hotspot NetworkManager profile and restore
#               any Wi-Fi profiles that setup_hotspot.sh --disable-others disabled.
# Repo files, logs/ and agv_run.sh are left alone; agv_run.sh still works.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "error: run with sudo." >&2; exit 1; }

HOTSPOT=0
[ "${1:-}" = "--hotspot" ] && HOTSPOT=1

systemctl disable --now agv-drive.service agv-web.service 2>/dev/null || true
rm -f /etc/systemd/system/agv-drive.service /etc/systemd/system/agv-web.service
systemctl daemon-reload
echo "Removed agv-drive and agv-web services."

if [ "$HOTSPOT" -eq 1 ]; then
  CON_NAME="agv-hotspot"
  [ -f "$HERE/hotspot.env" ] && CON_NAME="$(. "$HERE/hotspot.env"; echo "${HOTSPOT_CON_NAME:-agv-hotspot}")"
  nmcli connection delete "$CON_NAME" 2>/dev/null && echo "Deleted hotspot profile '$CON_NAME'." || echo "No hotspot profile '$CON_NAME' found."
  if [ -f "$HERE/.hotspot_disabled" ]; then
    while IFS=$'\t' read -r name prio; do
      [ -n "$name" ] || continue
      nmcli connection modify "$name" connection.autoconnect yes connection.autoconnect-priority "${prio:-0}" \
        && echo "Restored autoconnect: $name"
    done <"$HERE/.hotspot_disabled"
    rm -f "$HERE/.hotspot_disabled"
  fi
fi
