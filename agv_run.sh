#!/usr/bin/env bash
# Run the AGV that drives the Arduino chassis, from /home/deep/arduino/.
# Camera + decision + telemetry + web dashboard + Arduino — no STM, no
# LiDAR. The Arduino serial port defaults to /dev/ttyACM0.
#
# Usage:
#   ./agv_run.sh                              # foreground (Ctrl+C to stop)
#   AGV_ARDUINO_PORT=/dev/ttyUSB0 ./agv_run.sh   # different port
#   AGV_CAMERA_SENSOR_ID=1 ./agv_run.sh           # CAM1 instead of CAM0
#   AGV_SHOW_WINDOW=1 ./agv_run.sh                # local OpenCV preview
#
# To stop a backgrounded instance, send SIGINT to both PIDs:
#   kill -INT <agv_run.sh_pid> <web.py_pid>

# Recorder rollback switches (defaults are AGV_REC_FORMAT=v1, AGV_REC_REQUIRE_AUTH=1):
#   AGV_REC_FORMAT=v0  ./agv_run.sh          # old single-file recorder
#   AGV_REC_REQUIRE_AUTH=0 ./agv_run.sh      # recordings endpoints without sign-in

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}"
export AGV_DASHBOARD_DIR="${AGV_DASHBOARD_DIR:-/tmp/agv-dashboard}"
export AGV_SHOW_WINDOW="${AGV_SHOW_WINDOW:-0}"

LOG_DIR="${HERE}/logs"
mkdir -p "$LOG_DIR"

echo "Starting Arduino-driven AGV from $HERE"
echo "  AGV_ARDUINO_PORT = ${AGV_ARDUINO_PORT:-/dev/ttyACM0}"
echo "  AGV_DASHBOARD_DIR= $AGV_DASHBOARD_DIR"
echo "  AGV_CAMERA_SENSOR_ID = ${AGV_CAMERA_SENSOR_ID:-0}"
echo "  PYTHONPATH       = $PYTHONPATH"

# Start the web dashboard in the background, then run the orchestrator
# in the foreground. TelemetryPublisher writes status.json + frame.jpg
# into AGV_DASHBOARD_DIR; the dashboard reads from the same dir.
(
  cd "$HERE"
  /usr/bin/python3 web.py >>"$LOG_DIR/web.log" 2>&1 &
  WEB_PID=$!
  echo "web.py pid=$WEB_PID"
  echo "$WEB_PID" > "$LOG_DIR/web.pid"
  /usr/bin/python3 agv_drive_arduino.py >>"$LOG_DIR/agv.log" 2>&1
)
