"""Dashboard manual / driver-test command -> (v, omega) targets.

Shared by the full AGV (agv_drive_arduino.py) and the camera-less bench tool
(agv_motortest.py) so both turn the dashboard's control.json into wheel targets
in exactly the same way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config import MANUAL_TURN_FRAC
from control_state import MANUAL_STALE_S


@dataclass
class ManualCommand:
    v: float            # forward target, PWM units
    omega: float        # turn target, PWM units (+ = right)
    idle: bool          # nothing is being asked for
    fresh: bool         # the browser is still sending (deadman)
    raw: bool           # driver test: literal PWM, no PID / speed cap
    cmd: str            # F / B / L / R / S for the dashboard
    text: str


def manual_command(ctl: dict, speed_pct: float) -> ManualCommand:
    """Hold-to-drive (deadman): a command older than MANUAL_STALE_S counts as
    released. Goes through the same jerk limiter and max-speed cap as automatic
    driving, except a raw driver test (PWM = fraction x 255)."""
    fresh = (time.time() - ctl['manual_ts']) < MANUAL_STALE_S
    fwd = ctl['manual_v'] if fresh else 0.0
    trn = ctl['manual_w'] if fresh else 0.0
    cap = speed_pct / 100.0 * 255.0
    v_t, w_t = fwd * cap, trn * cap * MANUAL_TURN_FRAC
    raw = False
    if fresh and ctl.get('manual_l') is not None:
        # Per-wheel test (chassis diagram / driver test): exact left / right.
        lft, rgt = ctl['manual_l'], ctl['manual_r']
        fwd, trn = (lft + rgt) / 2.0, (lft - rgt) / 2.0
        raw = bool(ctl.get('manual_raw'))
        if raw:
            cap = 255.0
        v_t, w_t = fwd * cap, trn * cap
    idle = fwd == 0.0 and trn == 0.0
    cmd = ('S' if idle else 'F' if fwd > 0 else 'B' if fwd < 0
           else 'R' if trn > 0 else 'L')
    text = ('MANUAL: hold a key / button to drive' if idle
            else f'MANUAL v={fwd:+.1f} turn={trn:+.1f}')
    return ManualCommand(v_t, w_t, idle, fresh, raw, cmd, text)
