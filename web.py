"""Web dashboard for AGV telemetry.

Two surfaces, both reading the same telemetry directory:

  - HTTP / Flask (default port 5000):
      /            →  static dashboard page
      /status      →  JSON snapshot (used for first paint)
      /video_feed  →  MJPEG stream of the latest annotated frame
  - WebSocket (default port 5001):
      /            →  push the live status every ~500 ms
        Clients (a single browser tab) connect once and receive updates
        instead of polling. Lower CPU on the Jetson and smoother UI.

The HTTP layer is also a tiny server for the WebSocket — `flask run`
is not involved.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from config import (
    DASHBOARD_HOST, DASHBOARD_HTTP_PORT, DASHBOARD_WS_PORT, TELEMETRY_DIR,
)
from control_state import (
    MANUAL_STALE_S, ControlReader, clamp_speed, read_state, write_state,
)
from config import SPEED_MAX_PCT, SPEED_MIN_PCT

log = logging.getLogger('agv.web')

STATUS_PATH = Path(TELEMETRY_DIR) / 'status.json'
FRAME_PATH = Path(TELEMETRY_DIR) / 'frame.jpg'

# ─────────────────────────────────────────────────────────────────
# HTTP layer (Flask)
# ─────────────────────────────────────────────────────────────────

flask_app = Flask('agv-dashboard')


@flask_app.context_processor
def inject_project_config():
    """Make the project's `config.py` available to every template as
    `project_config`. Without this, `{{ config.X }}` in the template would
    resolve to Flask's own config object, not the AGV config module.
    """
    import config as _project_config
    return {'project_config': _project_config}


def _read_status() -> dict:
    fallback = {
        'running': False,
        'camera_connected': None,
        'camera_backend': 'argus',
        'stm32_connected': None,
        'stm32_ack_age_ms': 0.0,
        'stm32_encoder_left': 0,
        'stm32_encoder_right': 0,
        'stm32_encoder_delta_left': 0,
        'stm32_encoder_delta_right': 0,
        'stm32_firmware_version': '',
        'camera_direction': 'UNAVAILABLE',
        'camera_command': 'S',
        'line_centroid_x': -1,
        'line_centroid_y': -1,
        'line_area': 0,
        'line_error_px': 0,
        'lidar_status': 'UNKNOWN',
        'lidar_connected': None,
        'lidar_error': None,
        'lidar_distance_mm': 0,
        'final_command': 'S',
        'decision_reason': '',
        'updated_at': None,
        'logs': [],
        'sensor_probes': [],
        'last_error': None,
        'enabled': False,
        'max_speed_pct': 0,
        'mode': 'auto',
        'obstacle_enforced': True,
        'track_state': 'NO_LINE',
        'steer_boost': 1.0,
        'line_offset_m': 0.0,
        'line_angle_deg': 0.0,
        'line_lookahead_m': 0.0,
        'line_curvature': 0.0,
        'line_strips': 0,
        'obstacle_state': 'CLEAR',
        'obstacle_distance_m': 0.0,
        'speed_target': 0.0,
        'omega_target': 0.0,
        'left_pwm': 0,
        'right_pwm': 0,
        'vision_ms': 0.0,
    }
    try:
        with STATUS_PATH.open('r', encoding='utf-8') as fh:
            payload = json.load(fh)
        for key, default in fallback.items():
            payload.setdefault(key, default)
        return payload
    except (OSError, ValueError):
        return fallback


@flask_app.route('/', methods=['GET'])
def index():
    return render_template('dashboard.html')


_CONTROL = ControlReader()
PASSWORD_FILE = Path(__file__).resolve().parent / '.dashboard_password'


def _load_password() -> str:
    """Start password: env var, else .dashboard_password, else generate one."""
    env = os.environ.get('AGV_DASHBOARD_PASSWORD') or os.environ.get('AGV_DASHBOARD_TOKEN')
    if env:
        return env
    try:
        text = PASSWORD_FILE.read_text(encoding='utf-8').strip()
        if text:
            return text
    except OSError:
        pass
    generated = secrets.token_urlsafe(6)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(generated + '\n')
    log.warning('[API] generated a dashboard start password in %s', PASSWORD_FILE)
    return generated


_PASSWORD = _load_password()
_fail_times: list = []          # recent wrong-password timestamps


def _password_ok(req) -> tuple:
    """(ok, http_status). Wrong guesses are throttled: 5 in 60 s -> 30 s lockout."""
    now = time.monotonic()
    recent = [t for t in _fail_times if now - t < 60.0]
    _fail_times[:] = recent
    if len(recent) >= 5 and now - recent[-1] < 30.0:
        return False, 429
    given = req.headers.get('X-AGV-Password', '')
    if hmac.compare_digest(given.encode(), _PASSWORD.encode()):
        return True, 200
    _fail_times.append(now)
    return False, 403


def _control_json():
    state = read_state()
    state.pop('ts', None)
    state.pop('manual_ts', None)
    state.update(speed_min_pct=SPEED_MIN_PCT, speed_max_pct=SPEED_MAX_PCT,
                 password_required_to_start=True,
                 manual_stale_s=MANUAL_STALE_S)
    return jsonify(state)


@flask_app.route('/api/enable', methods=['GET', 'POST'])
def api_enable():
    """Motor switch. GET reads it; POST {"enabled": bool} sets it.

    START needs the dashboard password (``X-AGV-Password`` header). STOP never
    does, so the rover can always be switched off.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = bool(body.get('enabled', False))
        if want:
            ok, status = _password_ok(request)
            if not ok:
                msg = 'too many wrong passwords, wait' if status == 429 else 'wrong password'
                return jsonify({'error': msg}), status
        write_state(enabled=want)          # STOP also zeroes any manual command
        log.info('[API] motors %s from %s', 'ENABLED' if want else 'DISABLED',
                 request.remote_addr)
    return _control_json()


@flask_app.route('/api/mode', methods=['GET', 'POST'])
def api_mode():
    """Switch between automatic line following and manual driving.

    Going to MANUAL needs the dashboard password (it lets you drive the rover
    yourself). Going back to AUTO never does.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = 'manual' if body.get('mode') == 'manual' else 'auto'
        if want == 'manual':
            ok, status = _password_ok(request)
            if not ok:
                msg = 'too many wrong passwords, wait' if status == 429 else 'wrong password'
                return jsonify({'error': msg}), status
        write_state(mode=want)
        log.info('[API] mode %s from %s', want, request.remote_addr)
    return _control_json()


@flask_app.route('/api/manual', methods=['POST'])
def api_manual():
    """Manual drive command {"v": -1..1, "w": -1..1} (forward / turn right).

    Hold-to-drive: the browser repeats this ~10 times a second; the AGV treats
    a command older than MANUAL_STALE_S as released and stops. Needs the
    password and MANUAL mode with the motors ON.
    """
    ok, status = _password_ok(request)
    if not ok:
        return jsonify({'error': 'wrong password'}), status
    body = request.get_json(silent=True) or {}
    try:
        v, w = float(body.get('v', 0.0)), float(body.get('w', 0.0))
    except (TypeError, ValueError):
        return jsonify({'error': 'v and w must be numbers'}), 400
    state = read_state()
    if state['mode'] != 'manual' or not state['enabled']:
        return jsonify({'error': 'switch to MANUAL mode and press START first'}), 409
    write_state(manual=(v, w))
    return jsonify({'ok': True})


@flask_app.route('/api/obstacle', methods=['GET', 'POST'])
def api_obstacle():
    """Enforce (default) or ignore the camera obstacle stop.

    Switching it OFF removes a safety layer, so it needs the password;
    switching it back ON never does.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        want = bool(body.get('enabled', True))
        if not want:
            ok, status = _password_ok(request)
            if not ok:
                return jsonify({'error': 'wrong password'}), status
        write_state(obstacle_stop=want)
        log.info('[API] obstacle stop %s from %s', 'ON' if want else 'OFF', request.remote_addr)
    return _control_json()


@flask_app.route('/api/speed', methods=['GET', 'POST'])
def api_speed():
    """Master max-speed limit in percent. Every other speed scales with it."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        try:
            pct = clamp_speed(float(body.get('max_speed_pct')))
        except (TypeError, ValueError):
            return jsonify({'error': 'max_speed_pct must be a number'}), 400
        write_state(max_speed_pct=pct)
        log.info('[API] max speed %d%% from %s', pct, request.remote_addr)
    return _control_json()


@flask_app.route('/status', methods=['GET'])
def status():
    payload = _read_status()
    try:
        after_id = int(request.args.get('after', '0'))
    except ValueError:
        after_id = 0
    filtered = []
    for entry in payload.get('logs', []):
        if entry.get('id', 0) <= after_id:
            continue
        entry.setdefault('category', 'SYSTEM')
        filtered.append(entry)
    payload['logs'] = filtered
    return jsonify(payload)


def _generate_frames():
    while True:
        try:
            with FRAME_PATH.open('rb') as fh:
                frame = fh.read()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Cache-Control: no-cache\r\n\r\n' + frame + b'\r\n')
        except OSError:
            pass
        time.sleep(0.1)


@flask_app.route('/video_feed', methods=['GET'])
def video_feed():
    return Response(
        _generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


@flask_app.route('/frame.jpg', methods=['GET'])
def frame_jpg():
    """Serve the latest annotated frame as a plain JPEG (Playwright-friendly).

    The MJPEG stream at /video_feed is infinite, which makes it impossible
    for headless browsers to fetch it as a single asset. /frame.jpg is the
    one-shot version: it returns whatever bytes are currently in frame.jpg.
    """
    try:
        data = FRAME_PATH.read_bytes()
    except OSError:
        return Response(b'', status=204)
    return Response(
        data,
        mimetype='image/jpeg',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


@flask_app.route('/detections', methods=['GET'])
def detections():
    """Read-only detection viewer.

    Shows every signal agv_main.py reports: the annotated camera feed,
    the live centroid (cx, cy), the line-following verdict, the final motor
    command, the LiDAR safety state, the STM32 link health, and a running
    histogram of recent directions. All data is sourced from status.json.
    """
    return render_template('detections.html')


# ─────────────────────────────────────────────────────────────────
# WebSocket layer (push instead of polling)
# ─────────────────────────────────────────────────────────────────

class WSHub:
    """Tracks active WebSocket clients and broadcasts status snapshots."""

    def __init__(self) -> None:
        self._clients: set = set()
        self._lock = asyncio.Lock()

    async def register(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        encoded = json.dumps(message)
        dead = []
        for ws in clients:
            try:
                await ws.send(encoded)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


HUB = WSHub()


async def _ws_handler(ws) -> None:
    await HUB.register(ws)
    try:
        # Send first snapshot immediately, then await incoming pings.
        snapshot = _read_status()
        await ws.send(json.dumps(snapshot))
        async for _ in ws:
            pass  # client messages are unused; the server pushes only
    finally:
        await HUB.unregister(ws)


async def _publisher_loop() -> None:
    """Push status.json snapshots to every connected WS client at ~2 Hz."""
    last_id = 0
    while True:
        try:
            payload = _read_status()
            for entry in payload.get('logs', []):
                if entry.get('id', 0) > last_id:
                    last_id = entry['id']
            await HUB.broadcast(payload)
        except Exception as exc:
            log.warning('[WS] publish error: %s', exc)
        await asyncio.sleep(0.5)


async def _ws_main() -> None:
    import websockets
    async with websockets.serve(_ws_handler, DASHBOARD_HOST,
                                 DASHBOARD_WS_PORT,
                                 ping_interval=20, ping_timeout=20):
        await _publisher_loop()


def _start_ws() -> None:
    """Run the asyncio WebSocket loop on a dedicated daemon thread."""
    def _run():
        try:
            asyncio.run(_ws_main())
        except Exception as exc:
            log.warning('[WS] server stopped: %s', exc)
    threading.Thread(target=_run, name='ws-hub', daemon=True).start()


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    _start_ws()
    flask_app.run(
        host=DASHBOARD_HOST,
        port=DASHBOARD_HTTP_PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )


if __name__ == '__main__':
    main()