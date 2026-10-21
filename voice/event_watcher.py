"""Watch the main project's telemetry JSON files and emit persona events.

This module is the only place voice reaches into the rest of the
project. It reads ``control.json`` and ``points.json`` from the
telemetry directory the same way the dashboard does — atomically
(write-then-rename) — and converts file deltas into persona events.

**Nothing in this package imports from the main project.** We read
JSON, not Python. The main project stays untouched.

Detected transitions (Phase 0):

    control.json
        enabled False→True             'motors_enabled'  (mode slot)
        enabled True→False             'motors_disabled'
        mode flipped while enabled     'mode_changed'    (mode slot)
        obstacle_stop True→False       'obstacle_stop_disabled'  (password override)
        lidar_enabled True→False       'lidar_disabled'          (password override)

    points.json (debounced HOLD_DEBOUNCE_S)
        stop-zone points ≥ threshold   'lidar_hold'     (reason='obstacle ahead')
        stop-zone cleared              'lidar_clear'

Every transition fires exactly once. The first reading after
construction is *not* narrated — we don't want Tarka to introduce
herself on every (re)start.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ._constants import (
    CONTROL_FILENAME,
    HOLD_DEBOUNCE_S,
    HOLD_MIN_STOP_POINTS,
    PERSONA_EVENTS,
    POINTS_FILENAME,
)
from .persona import render_event

log = logging.getLogger(__name__)


class Speaker(Protocol):
    """Anything ``EventWatcher`` can hand an utterance to.

    Both ``TtsEngine`` and ``SilentTts`` satisfy this — they have a
    ``speak_event(event, utterance)`` method.
    """
    def speak_event(self, event: str, utterance: str): ...


@dataclass
class _HoldState:
    """LiDAR stop-zone debounce state."""
    last_seen_ts: Optional[float] = None    # when we first saw the obstacle
    active: bool = False                   # have we already fired lidar_hold?


class EventWatcher:
    """Watch ``control.json`` + ``points.json`` and speak persona utterances.

    A ``tick()`` does one poll iteration. Use ``run_forever()`` to drive
    a blocking loop, or call ``tick()`` from your own scheduler. The
    watcher never writes to either file.
    """

    def __init__(self,
                 telemetry_dir: str,
                 speaker: Speaker,
                 poll_interval_s: float = 0.2,
                 control_filename: str = CONTROL_FILENAME,
                 points_filename: str = POINTS_FILENAME,
                 hold_debounce_s: float = HOLD_DEBOUNCE_S,
                 hold_min_stop_points: int = HOLD_MIN_STOP_POINTS) -> None:
        self._dir = Path(telemetry_dir)
        self._speaker = speaker
        self._poll_s = poll_interval_s
        self._control_path = self._dir / control_filename
        self._points_path = self._dir / points_filename
        self._hold_debounce_s = hold_debounce_s
        self._hold_min = hold_min_stop_points

        self._last_control: Optional[dict] = None
        self._last_points: Optional[dict] = None
        self._hold = _HoldState()

    def tick(self) -> None:
        """One poll iteration. Idempotent and crash-safe on bad files."""
        self._tick_control()
        self._tick_points()

    def run_forever(self) -> None:
        """Blocking loop. Stops on KeyboardInterrupt (Ctrl-C)."""
        log.info('event watcher running on %s', self._dir)
        try:
            while True:
                self.tick()
                time.sleep(self._poll_s)
        except KeyboardInterrupt:
            log.info('event watcher stopped')

    # ── control.json ────────────────────────────────────────────────

    def _tick_control(self) -> None:
        cur = self._read_json(self._control_path)
        if cur is None:
            return
        prev = self._last_control
        self._last_control = cur
        if prev is None:
            return     # first reading — never narrate the initial state

        prev_enabled = bool(prev.get('enabled', False))
        cur_enabled = bool(cur.get('enabled', False))
        if cur_enabled and not prev_enabled:
            mode = 'manual' if cur.get('mode') == 'manual' else 'auto'
            self._fire('motors_enabled', mode=mode)
        elif prev_enabled and not cur_enabled:
            self._fire('motors_disabled')

        # mode change while the rover is enabled
        if cur_enabled and cur.get('mode') != prev.get('mode'):
            mode = 'manual' if cur.get('mode') == 'manual' else 'auto'
            self._fire('mode_changed', mode=mode)

        # password-gated toggles
        if prev.get('obstacle_stop') and not cur.get('obstacle_stop'):
            self._fire('obstacle_stop_disabled')
        if prev.get('lidar_enabled') and not cur.get('lidar_enabled'):
            self._fire('lidar_disabled')

    # ── points.json ─────────────────────────────────────────────────

    def _tick_points(self) -> None:
        cur = self._read_json(self._points_path)
        if cur is None:
            return
        prev = self._last_points
        self._last_points = cur
        if prev is None:
            return

        stop_count = self._count_stop_points(cur)
        now = time.monotonic()

        # entered hold
        if stop_count >= self._hold_min and not self._hold.active:
            if self._hold.last_seen_ts is None:
                self._hold.last_seen_ts = now
            elif now - self._hold.last_seen_ts >= self._hold_debounce_s:
                self._hold.active = True
                self._fire('lidar_hold', reason='obstacle ahead')
                self._hold.last_seen_ts = None
        # exited hold
        elif stop_count == 0 and self._hold.active:
            self._hold.active = False
            self._hold.last_seen_ts = None
            self._fire('lidar_clear')
        else:
            # oscillating — keep restarting the timer
            if stop_count == 0:
                self._hold.last_seen_ts = None

    @staticmethod
    def _count_stop_points(points_payload: dict) -> int:
        points = points_payload.get('points') or []
        return sum(1 for p in points if p.get('zone') == 'stop')

    # ── shared ──────────────────────────────────────────────────────

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _fire(self, event: str, **slots) -> None:
        if event not in PERSONA_EVENTS:
            log.debug('dropping unknown event %r', event)
            return
        utterance = render_event(event, **slots)
        if utterance is None:
            return
        self._speaker.speak_event(event, utterance)
        log.info('event=%s utterance=%r', event, utterance)


__all__ = [
    'EventWatcher',
    'Speaker',
    'CONTROL_FILENAME',
    'POINTS_FILENAME',
    'HOLD_DEBOUNCE_S',
    'HOLD_MIN_STOP_POINTS',
]
