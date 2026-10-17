"""Jerk-limited wheel command shaping.

The vision loop only decides *targets* (forward speed ``v`` and turn ``omega``,
both in PWM units). This module turns them into wheel PWM at a fixed rate with
bounded acceleration AND bounded jerk, so starts, stops and steering changes
follow an S-curve instead of a step.

Terms (PWM stands in for speed):
  level  PWM                 what we send to the wheel
  rate   PWM / s             "acceleration"   - limited by max_accel
  jerk   PWM / s^2 per s     change of rate   - limited by max_jerk

``omega > 0`` means turn right: left wheel faster, right wheel slower.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

from config import (
    MAX_ACCEL, MAX_JERK, MIN_PWM, MOTION_HZ, OMEGA_TAU_S, STOP_ACCEL,
    STOP_JERK, TARGET_STALE_S,
)

log = logging.getLogger('agv.motion')


class JerkLimiter:
    """Tracks a target with |rate| <= max_accel and |d(rate)/dt| <= max_jerk."""

    def __init__(self) -> None:
        self.level = 0.0
        self.rate = 0.0

    def step(self, target: float, dt: float,
             max_accel: float, max_jerk: float) -> float:
        err = target - self.level
        if abs(err) < 0.5 and abs(self.rate) < max_jerk * dt:
            self.level, self.rate = target, 0.0
            return self.level
        # Fastest rate from which a jerk-limited ramp of the rate back to
        # zero still lands on the target (no overshoot).
        wanted = math.copysign(
            min(max_accel, math.sqrt(1.2 * max_jerk * abs(err))), err)
        max_dr = max_jerk * dt
        self.rate += max(-max_dr, min(max_dr, wanted - self.rate))
        prev_err = err
        self.level += self.rate * dt
        if (target - self.level) * prev_err < 0:      # crossed the target
            self.level, self.rate = target, 0.0
        return self.level


def _to_pwm(level: float) -> int:
    """Level (-255..255) -> wheel PWM with the stiction dead band removed."""
    mag = abs(level)
    if mag < 1.0:
        return 0
    out = MIN_PWM + (255.0 - MIN_PWM) * min(mag, 255.0) / 255.0
    return int(round(math.copysign(out, level)))


@dataclass
class WheelCommand:
    left: int = 0
    right: int = 0
    left_level: float = 0.0
    right_level: float = 0.0


class MotionController:
    """(v, omega) targets -> smooth left/right PWM, call at a fixed rate."""

    def __init__(self) -> None:
        self._left = JerkLimiter()
        self._right = JerkLimiter()
        self._omega = 0.0

    @property
    def idle(self) -> bool:
        return (abs(self._left.level) < 0.5 and abs(self._right.level) < 0.5)

    def step(self, v: float, omega: float, emergency: bool,
             dt: float) -> WheelCommand:
        if emergency:
            v, omega = 0.0, 0.0
        # Low-pass the steering so a noisy line fit does not twitch the wheels.
        a = dt / (OMEGA_TAU_S + dt)
        self._omega += (omega - self._omega) * a
        if emergency:
            self._omega = 0.0
        w = max(-255.0, min(255.0, self._omega))
        # Keep the turn: when v + |w| would exceed full PWM, give up forward
        # speed instead of clipping one wheel (which would flatten the turn).
        if abs(v) + abs(w) > 255.0:
            v = math.copysign(max(0.0, 255.0 - abs(w)), v)
        left_t = max(-255.0, min(255.0, v + w))
        right_t = max(-255.0, min(255.0, v - w))
        accel = STOP_ACCEL if emergency else MAX_ACCEL
        jerk = STOP_JERK if emergency else MAX_JERK
        left = self._left.step(left_t, dt, accel, jerk)
        right = self._right.step(right_t, dt, accel, jerk)
        return WheelCommand(_to_pwm(left), _to_pwm(right), left, right)


class MotionThread(threading.Thread):
    """Fixed-rate loop that owns the Arduino motor output.

    Vision runs at the camera's pace and only calls ``set_target``. This
    thread keeps sending shaped PWM at ``MOTION_HZ`` regardless, and falls
    back to a (bounded) emergency stop if the targets go stale, the Arduino
    stops answering, or the dashboard switch is off.
    """

    def __init__(self, arduino, hz: float = MOTION_HZ) -> None:
        super().__init__(name='motion', daemon=True)
        self._arduino = arduino
        self._period = 1.0 / max(hz, 10.0)
        self._motion = MotionController()
        self._lock = threading.Lock()
        self._target = (0.0, 0.0, True, 0.0)     # v, omega, emergency, stamp
        self._enabled = False
        self._stopping = False
        self.last = WheelCommand()
        self.why_stopped = 'start'
        self._last_err = 0.0

    def set_target(self, v: float, omega: float, emergency: bool,
                   enabled: bool) -> None:
        with self._lock:
            self._target = (v, omega, emergency, time.monotonic())
            self._enabled = enabled

    def request_stop(self) -> None:
        with self._lock:
            self._stopping = True

    def snapshot(self) -> WheelCommand:
        with self._lock:
            return self.last

    def run(self) -> None:
        last = time.monotonic()
        next_t = last
        while True:
            now = time.monotonic()
            dt = min(max(now - last, 0.005), 0.1)
            last = now
            with self._lock:
                v, omega, emergency, stamp = self._target
                enabled, stopping = self._enabled, self._stopping
            why = ''
            if stopping:
                why = 'shutting down'
            elif not enabled:
                why = 'dashboard OFF'
            elif now - stamp > TARGET_STALE_S:
                why = 'vision stalled'
            elif not self._arduino.link_alive():
                why = 'arduino link'
            if why:
                v, omega, emergency = 0.0, 0.0, True
            try:
                cmd = self._motion.step(v, omega, emergency, dt)
                self._arduino.send_motors(cmd.left, cmd.right)
            except Exception as exc:                      # keep the loop alive
                if now - self._last_err > 2.0:            # 50 Hz: do not spam
                    self._last_err = now
                    log.error('[Motion] %s: %s', type(exc).__name__, exc)
                cmd = WheelCommand()
            with self._lock:
                self.last = cmd
                self.why_stopped = why
            if stopping and self._motion.idle:
                self._arduino.send_motors(0, 0, force=True)
                return
            next_t += self._period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()
