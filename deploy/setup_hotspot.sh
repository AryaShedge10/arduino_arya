#!/usr/bin/env bash
# Create or update the AGV's own Wi-Fi hotspot as a NetworkManager profile.
# Idempotent: re-running updates the existing profile, never duplicates it.
#
# Usage (on the Jetson):
#   cp deploy/hotspot.env.example deploy/hotspot.env   # then edit it
#   sudo ./deploy/setup_hotspot.sh [--disable-others] [--no-up]
#
#   --disable-others  stop other saved Wi-Fi profiles from auto-connecting
#                     (recorded in deploy/.hotspot_disabled so uninstall.sh
#                     can restore them). Default: only list them.
#   --no-up           save the profile but do not activate it now.
#
# WARNING: activating the hotspot takes the Wi-Fi card out of client mode.
# If you are connected over Wi-Fi/SSH right now, that session will drop.
# Use Ethernet/USB for SSH, or pass --no-up and reboot.
#
# Address: 10.42.0.1/24 (the AGV dashboard is http://10.42.0.1:5000).
# Uses nmcli only. The passphrase is passed to nmcli as an argument, so it is
# briefly visible in `ps` to local users on this machine.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${HOTSPOT_ENV_FILE:-$HERE/hotspot.env}"
DISABLED_LOG="$HERE/.hotspot_disabled"
HOTSPOT_ADDR="10.42.0.1/24"

DISABLE_OTHERS=0
BRING_UP=1
for arg in "$@"; do
  case "$arg" in
    --disable-others) DISABLE_OTHERS=1 ;;
    --no-up) BRING_UP=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "error: unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (nmcli needs root to save system profiles)."
command -v nmcli >/dev/null || die "nmcli not found (NetworkManager is required)."
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found. Copy deploy/hotspot.env.example to deploy/hotspot.env and edit it."

if [ -n "$(find "$ENV_FILE" -perm /004 2>/dev/null)" ]; then
  echo "warning: $ENV_FILE is world-readable; run: chmod 600 $ENV_FILE" >&2
fi

HOTSPOT_SSID="" HOTSPOT_PASSWORD="" HOTSPOT_IFACE="" HOTSPOT_BAND=""
HOTSPOT_CON_NAME="agv-hotspot"
# shellcheck disable=SC1090
. "$ENV_FILE"

[ -n "$HOTSPOT_SSID" ] || die "HOTSPOT_SSID is empty in $ENV_FILE."
[ "${#HOTSPOT_SSID}" -le 32 ] || die "HOTSPOT_SSID must be at most 32 characters."
if [ "${#HOTSPOT_PASSWORD}" -lt 8 ]; then
  die "HOTSPOT_PASSWORD must be at least 8 characters (WPA2 minimum); got ${#HOTSPOT_PASSWORD}."
fi
[ "${#HOTSPOT_PASSWORD}" -le 63 ] || die "HOTSPOT_PASSWORD must be at most 63 characters."

# Hardware check: the Wi-Fi card must support access-point mode.
if command -v iw >/dev/null; then
  modes="$(iw list 2>/dev/null | awk '/Supported interface modes/{f=1;next} f&&/^[ \t]+\* /{print;next} f{exit}')"
  printf '%s\n' "$modes" | grep -Eq '^[[:space:]]+\* AP$' \
    || die "'iw list' does not show AP under 'Supported interface modes'. This card cannot host a hotspot. Stopping."
else
  echo "warning: 'iw' not installed; cannot confirm AP mode support." >&2
fi

# Interface: use the configured one, else the only Wi-Fi device. Never guess wlan0.
if [ -z "$HOTSPOT_IFACE" ]; then
  mapfile -t wifis < <(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1}')
  [ "${#wifis[@]}" -eq 1 ] || die "found ${#wifis[@]} Wi-Fi devices; set HOTSPOT_IFACE in $ENV_FILE (see 'nmcli device')."
  HOTSPOT_IFACE="${wifis[0]}"
fi
nmcli -t -f DEVICE,TYPE device | grep -qx "$HOTSPOT_IFACE:wifi" \
  || die "'$HOTSPOT_IFACE' is not a Wi-Fi device in 'nmcli device'."

# Other saved Wi-Fi profiles compete for the same radio.
others=()
while IFS=: read -r name type; do
  [ "$type" = "802-11-wireless" ] && [ "$name" != "$HOTSPOT_CON_NAME" ] && others+=("$name")
done < <(nmcli -t -f NAME,TYPE connection show)

if [ "${#others[@]}" -gt 0 ]; then
  echo "Other saved Wi-Fi profiles (they can win the interface at boot):"
  for n in "${others[@]}"; do
    echo "  - $n (autoconnect=$(nmcli -g connection.autoconnect connection show "$n"), priority=$(nmcli -g connection.autoconnect-priority connection show "$n"))"
  done
  if [ "$DISABLE_OTHERS" -eq 1 ]; then
    : >"$DISABLED_LOG"
    for n in "${others[@]}"; do
      printf '%s\t%s\n' "$n" "$(nmcli -g connection.autoconnect-priority connection show "$n")" >>"$DISABLED_LOG"
      nmcli connection modify "$n" connection.autoconnect no connection.autoconnect-priority 0
      echo "  disabled autoconnect: $n"
    done
  else
    echo "Not changed. Re-run with --disable-others to stop them auto-connecting."
  fi
fi

props=(
  connection.autoconnect yes
  connection.autoconnect-priority 100
  connection.interface-name "$HOTSPOT_IFACE"
  802-11-wireless.ssid "$HOTSPOT_SSID"
  802-11-wireless.mode ap
  wifi-sec.key-mgmt wpa-psk
  wifi-sec.proto rsn
  wifi-sec.pairwise ccmp
  wifi-sec.group ccmp
  wifi-sec.psk "$HOTSPOT_PASSWORD"
  ipv4.method shared
  ipv4.addresses "$HOTSPOT_ADDR"
  ipv6.method ignore
)
[ -z "$HOTSPOT_BAND" ] || props+=(802-11-wireless.band "$HOTSPOT_BAND")

if nmcli -t -f NAME connection show | grep -qx "$HOTSPOT_CON_NAME"; then
  echo "Updating existing profile '$HOTSPOT_CON_NAME'."
  nmcli connection modify "$HOTSPOT_CON_NAME" "${props[@]}"
else
  echo "Creating profile '$HOTSPOT_CON_NAME'."
  nmcli connection add type wifi con-name "$HOTSPOT_CON_NAME" ifname "$HOTSPOT_IFACE" ssid "$HOTSPOT_SSID"
  nmcli connection modify "$HOTSPOT_CON_NAME" "${props[@]}"
fi

if [ "$BRING_UP" -eq 1 ]; then
  echo "Activating (a Wi-Fi SSH session will drop now)..."
  nmcli connection up "$HOTSPOT_CON_NAME" ifname "$HOTSPOT_IFACE"
fi

cat <<MSG

Hotspot profile '$HOTSPOT_CON_NAME' ready: SSID '$HOTSPOT_SSID' on $HOTSPOT_IFACE, $HOTSPOT_ADDR, autoconnect on.
Check:  nmcli connection show --active ; ip -4 addr show $HOTSPOT_IFACE
Phone:  join '$HOTSPOT_SSID', open http://10.42.0.1:5000
MSG
