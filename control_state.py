"""Dashboard controls shared between web.py and the AGV process.

web.py writes ``control.json`` into the telemetry directory; the AGV reads
it every tick. Two settings live there:

  enabled        motors ON/OFF. The AGV resets this to OFF on startup, so a
                 restart can never start driving by itself.
  mode           'auto' (line following) or 'manual' (dashboard driving).
  manual_v/_w    manual drive command, -1..1 (forward / turn right) plus its
                 wall-clock timestamp; the AGV treats it as stale after
                 MANUAL_STALE_S, so releasing a button (or losing the browser)
                 stops the rover.
  obstacle_stop  True = camera obstacle STOP/SLOW is enforced (default).
                 Turning it off needs the password; for bench testing only.
  max_speed_pct  master speed limit, 10-100 % of full PWM. Every other speed
                 (line following, caution, obstacle slow-down, turning,
                 search) is scaled by it. Survives restarts.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time

from config import (
    DEFAULT_SPEED_PCT, SPEED_MAX_PCT, SPEED_MIN_PCT, TELEMETRY_DIR,
)

CONTROL_PATH = os.path.join(TELEMETRY_DIR, 'control.json')
MANUAL_STALE_S = 0.4


def clamp_speed(pct: float) -> int:
    return int(round(max(SPEED_MIN_PCT, min(SPEED_MAX_PCT, float(pct)))))


def _unit(x) -> float:
    return max(-1.0, min(1.0, float(x)))


def read_state(path: str = CONTROL_PATH) -> dict:
    """Current controls; unreadable / missing file means OFF + default speed."""
    state = {'enabled': False, 'max_speed_pct': DEFAULT_SPEED_PCT,
             'obstacle_stop': True, 'mode': 'auto', 'manual_v': 0.0, 'manual_w': 0.0, 'manual_ts': 0.0}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        state['enabled'] = bool(raw.get('enabled', False))
        state['max_speed_pct'] = clamp_speed(raw.get('max_speed_pct', DEFAULT_SPEED_PCT))
        state['mode'] = 'manual' if raw.get('mode') == 'manual' else 'auto'
        state['obstacle_stop'] = bool(raw.get('obstacle_stop', True))
        state['manual_v'] = _unit(raw.get('manual_v', 0.0))
        state['manual_w'] = _unit(raw.get('manual_w', 0.0))
        state['manual_ts'] = float(raw.get('manual_ts', 0.0))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return state


def write_state(enabled=None, max_speed_pct=None, mode=None, manual=None,
                obstacle_stop=None, path: str = CONTROL_PATH) -> dict:
    """Update only the given fields, atomically; returns the new state.

    ``manual`` is a (v, w) pair in -1..1. Stopping (enabled=False) or leaving
    manual mode always zeroes the manual command. A file lock keeps the
    read-modify-write safe against the 10 Hz manual updates.
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        if enabled is not None:
            state['enabled'] = bool(enabled)
            if not enabled:
                state['manual_v'] = state['manual_w'] = 0.0
        if max_speed_pct is not None:
            state['max_speed_pct'] = clamp_speed(max_speed_pct)
        if obstacle_stop is not None:
            state['obstacle_stop'] = bool(obstacle_stop)
        if mode is not None:
            state['mode'] = 'manual' if mode == 'manual' else 'auto'
            if state['mode'] != 'manual':
                state['manual_v'] = state['manual_w'] = 0.0
        if manual is not None:
            state['manual_v'], state['manual_w'] = _unit(manual[0]), _unit(manual[1])
            state['manual_ts'] = time.time()
        state['ts'] = time.time()
        fd, tmp = tempfile.mkstemp(prefix='.ctl-', dir=os.path.dirname(path) or '.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return state


def write_enabled(enabled: bool, path: str = CONTROL_PATH) -> None:
    """Used at AGV startup: OFF, automatic mode, no manual command."""
    write_state(enabled=enabled, mode='auto' if not enabled else None, path=path)


class ControlReader:
    """Reads the tiny control file on every call (no mtime caching: file
    timestamps are coarse, so two quick writes could look unchanged)."""

    def __init__(self, path: str = CONTROL_PATH) -> None:
        self.path = path

    def state(self) -> dict:
        return read_state(self.path)

    def enabled(self) -> bool:
        return self.state()['enabled']
