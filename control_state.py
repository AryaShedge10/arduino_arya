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
  manual_l/_r    optional per-wheel test command, -1..1 of the speed cap
                 (overrides manual_v/_w while fresh). Used by the dashboard's
                 chassis diagram to spin one side at a time.
  manual_raw     True = the per-wheel test is a raw driver command (PWM =
                 fraction x 255, no speed cap, no PID). False = a speed
                 command (fraction of the max-speed cap, PID holds it).
  enc_zero_ts    wall-clock time of the last "zero encoder counts" request; the
                 running process zeroes its tick display when this changes.
  record         True = the AGV is saving test frames + a detection log (see
                 recorder.py). record_rate / record_views set how; record_mark_ts +
                 record_mark_note flag "this frame is wrong". Reset to OFF at startup.
                 record_mode is 'continuous' (every frame at record_rate) or
                 'triggered' (ring buffer + event clips); anything else, or a
                 missing key in an old file, reads as config.REC_MODE_DEFAULT.
  closed_loop    True = wheel speed PID on the Arduino (encoders), False = raw
                 PWM. Only has effect while the firmware reports support.
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
    CLOSED_LOOP, DEFAULT_SPEED_PCT, REC_DEFAULT_RATE_HZ, REC_MODE_DEFAULT, SPEED_MAX_PCT, SPEED_MIN_PCT, TELEMETRY_DIR,
)

CONTROL_PATH = os.path.join(TELEMETRY_DIR, 'control.json')
MANUAL_STALE_S = 0.4
REC_MODES = ('continuous', 'triggered')
_DEFAULT_REC_MODE = REC_MODE_DEFAULT if REC_MODE_DEFAULT in REC_MODES else 'continuous'


def _rec_mode(value) -> str:
    return value if value in REC_MODES else _DEFAULT_REC_MODE


def clamp_speed(pct: float) -> int:
    return int(round(max(SPEED_MIN_PCT, min(SPEED_MAX_PCT, float(pct)))))


def _unit(x) -> float:
    return max(-1.0, min(1.0, float(x)))


def read_state(path: str = CONTROL_PATH) -> dict:
    """Current controls; unreadable / missing file means OFF + default speed."""
    state = {'enabled': False, 'max_speed_pct': DEFAULT_SPEED_PCT,
             'obstacle_stop': True, 'mode': 'auto', 'manual_v': 0.0, 'manual_w': 0.0, 'manual_ts': 0.0,
             'manual_l': None, 'manual_r': None, 'manual_raw': False, 'enc_zero_ts': 0.0,
             'record': False, 'record_rate': REC_DEFAULT_RATE_HZ, 'record_views': False,
             'record_mark_ts': 0.0, 'record_mark_note': '', 'record_mode': _DEFAULT_REC_MODE,
             'closed_loop': CLOSED_LOOP}
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
        state['closed_loop'] = bool(raw.get('closed_loop', CLOSED_LOOP))
        state['enc_zero_ts'] = float(raw.get('enc_zero_ts', 0.0))
        state['record'] = bool(raw.get('record', False))
        state['record_rate'] = max(0.2, min(60.0, float(raw.get('record_rate', REC_DEFAULT_RATE_HZ))))
        state['record_views'] = bool(raw.get('record_views', False))
        state['record_mode'] = _rec_mode(raw.get('record_mode'))
        state['record_mark_ts'] = float(raw.get('record_mark_ts', 0.0))
        state['record_mark_note'] = str(raw.get('record_mark_note', ''))[:200]
        if raw.get('manual_l') is not None and raw.get('manual_r') is not None:
            state['manual_l'], state['manual_r'] = _unit(raw['manual_l']), _unit(raw['manual_r'])
            state['manual_raw'] = bool(raw.get('manual_raw', False))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return state


def write_state(enabled=None, max_speed_pct=None, mode=None, manual=None,
                obstacle_stop=None, manual_lr=None, closed_loop=None, raw=False, enc_zero=False,
                record=None, record_rate=None, record_views=None, record_mark=None,
                record_mode=None, path: str = CONTROL_PATH) -> dict:
    """Update only the given fields, atomically; returns the new state.

    ``manual`` is a (v, w) pair in -1..1; ``manual_lr`` a (left, right) pair. Stopping (enabled=False) or leaving
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
                state['manual_l'] = state['manual_r'] = None
                state['manual_raw'] = False
                state['closed_loop'] = CLOSED_LOOP
        if max_speed_pct is not None:
            state['max_speed_pct'] = clamp_speed(max_speed_pct)
        if obstacle_stop is not None:
            state['obstacle_stop'] = bool(obstacle_stop)
        if mode is not None:
            state['mode'] = 'manual' if mode == 'manual' else 'auto'
            if state['mode'] != 'manual':
                state['manual_v'] = state['manual_w'] = 0.0
                state['manual_l'] = state['manual_r'] = None
                state['manual_raw'] = False
                state['closed_loop'] = CLOSED_LOOP      # bench test settings never reach AUTO
        if manual is not None:
            state['manual_v'], state['manual_w'] = _unit(manual[0]), _unit(manual[1])
            state['manual_l'] = state['manual_r'] = None
            state['manual_raw'] = False
            state['manual_ts'] = time.time()
        if manual_lr is not None:
            state['manual_l'], state['manual_r'] = _unit(manual_lr[0]), _unit(manual_lr[1])
            state['manual_v'] = state['manual_w'] = 0.0
            state['manual_raw'] = bool(raw)
            state['manual_ts'] = time.time()
        if closed_loop is not None:
            state['closed_loop'] = bool(closed_loop)
        if enc_zero:
            state['enc_zero_ts'] = time.time()
        if record is not None:
            state['record'] = bool(record)
        if record_rate is not None:
            state['record_rate'] = max(0.2, min(60.0, float(record_rate)))
        if record_views is not None:
            state['record_views'] = bool(record_views)
        if record_mode is not None:
            state['record_mode'] = _rec_mode(record_mode)   # invalid -> default
        if record_mark is not None:
            state['record_mark_ts'] = time.time()
            state['record_mark_note'] = str(record_mark)[:200]
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
