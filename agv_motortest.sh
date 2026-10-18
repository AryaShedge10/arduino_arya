#!/usr/bin/env bash
# Motor / encoder bench test: Arduino + dashboard only (no camera, no vision).
#
#   ./agv_motortest.sh          # Ctrl+C stops the motors and the dashboard
#
# Open http://<jetson-ip>:5000 -> Motor test tab -> sign in -> "Enable motor
# test". Cannot run together with ./agv_run.sh (both need the Arduino port).

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}"
export AGV_DASHBOARD_DIR="${AGV_DASHBOARD_DIR:-/tmp/agv-dashboard}"
LOG_DIR="${HERE}/logs"
mkdir -p "$LOG_DIR"

if pgrep -f agv_drive_arduino.py >/dev/null; then
  echo "The full AGV is already running (it owns the Arduino)." >&2
  echo "Use its dashboard Motor test tab, or stop it first." >&2
  exit 2
fi

cd "$HERE"
echo "Motor test: Arduino ${AGV_ARDUINO_PORT:-/dev/ttyACM0}, dashboard http://$(hostname -I | awk '{print $1}'):5000"
if ! pgrep -f "python3 web.py" >/dev/null; then
  /usr/bin/python3 web.py >>"$LOG_DIR/web.log" 2>&1 &
  WEB_PID=$!
  trap 'kill $WEB_PID 2>/dev/null || true' EXIT
fi
/usr/bin/python3 agv_motortest.py
