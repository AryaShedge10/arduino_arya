"""points.json publisher — feeds the dashboard's polar overlay.

Runs its own thread at ~10 Hz and atomically rewrites
``<out_dir>/points.json`` with the LiDAR's recent raw returns plus the
stop/slow zone distances currently in force. The dashboard polls the
file every 250 ms (see the ``tick()`` loop in ``_lidar_tab.html``) and
shades the two zone discs from those distances, so they must be in the
payload — without them the canvas pins the rings at its startup
defaults no matter what the operator tunes.

Zone distances come from ``zones_source``, a callable re-read on every
tick. Capturing them once at construction time would freeze the rings
until the publisher was restarted.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from ._constants import POINTS_MAX, POINTS_WINDOW_S

log = logging.getLogger('agv.lidar')

# Emit at most one warning per this many seconds, so a persistent fault
# (read-only mount, bad path) doesn't flood the log at 10 Hz.
_WARN_EVERY_S = 10.0


class PointsPublisher:
    """Background writer for the dashboard's ``points.json``.

    ``lidar`` only has to expose ``recent_points(window_s, limit)`` —
    anything with that method works, which keeps the publisher testable
    without a sensor.
    """

    def __init__(self, lidar,
                 out_dir: str,
                 zones_source: Callable[[], tuple],
                 interval_s: float = 0.1,
                 window_s: float = POINTS_WINDOW_S,
                 limit: int = POINTS_MAX) -> None:
        self._lidar = lidar
        self._out_dir = out_dir
        self._zones_source = zones_source
        self._interval_s = interval_s
        self._window_s = window_s
        self._limit = limit
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_warn = 0.0

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> 'PointsPublisher':
        """Create the output directory and spawn the writer thread."""
        os.makedirs(self._out_dir, exist_ok=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name='lidar-points-writer', daemon=True)
        self._thread.start()
        log.info('points.json publisher started -> %s', self._out_path)
        return self

    def stop(self) -> None:
        """Signal the writer and join it. Idempotent."""
        if self._stop.is_set():
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @property
    def _out_path(self) -> str:
        return os.path.join(self._out_dir, 'points.json')

    # ── writer ───────────────────────────────────────────────────────
    def publish_once(self) -> dict:
        """Build and atomically write one payload. Returns it.

        Safe to call directly (the tests do); ``_loop`` just calls this
        on a timer.
        """
        out_path = self._out_path
        tmp_path = out_path + '.tmp'
        stop_dist_m, slow_dist_m = self._zones_source()

        pts_all = self._lidar.recent_points(self._window_s, self._limit)
        # Anchor t=0 on the oldest point still in the window so the
        # dashboard can map ages onto its own clock with one offset.
        t0 = pts_all[0][0] if pts_all else 0.0
        points = [
            {
                't': round(p[0] - t0, 4),
                'a': round(float(p[1]), 3),
                'd': round(float(p[2]), 4),
                'zone': ('stop' if p[2] < stop_dist_m
                         else 'slow' if p[2] < slow_dist_m
                         else 'clear'),
            }
            for p in pts_all
        ]
        payload = {
            'updated_at': time.time(),   # display only; not a sensor clock
            'window_s': self._window_s,
            'lidar_stop_dist_m': stop_dist_m,
            'lidar_slow_dist_m': slow_dist_m,
            'n': len(points),
            'points': points,
        }
        # Write-then-rename: a reader never sees a half-written file.
        with open(tmp_path, 'w') as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, out_path)
        return payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_warn >= _WARN_EVERY_S:
                    self._last_warn = now
                    log.warning('points.json writer tick failed: %s', exc)
            self._stop.wait(self._interval_s)   # wakes early on stop()

    def __repr__(self) -> str:
        state = 'running' if (self._thread and self._thread.is_alive()) else 'stopped'
        return f'<PointsPublisher {state} -> {self._out_path}>'


__all__ = ['PointsPublisher']