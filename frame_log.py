"""Append-only JSONL writer for the dashboard's per-tick live feed.

One file per local date: ``<base_dir>/agv-frames-YYYY-MM-DD.jsonl``. The
control loop calls ``write(row)`` every tick; the module rate-limits so the
caller does not have to, and rotates at local midnight.

Public API:

* ``FrameLog(base_dir, hz=5.0)``     — config only, no files opened.
* ``frame_log.start()``              — opens today's file in append mode.
* ``frame_log.write(row) -> bool``   — rate-limited, appends one JSON line.
* ``frame_log.close()``              — idempotent; flushes + closes.
* ``frame_log.current_path``         — today's file (or ``''`` when closed).

Thread safety: one writer thread (AGV control loop) + one closer (shutdown).
A single ``threading.Lock`` covers every state mutation. The rate-limit gate
and the daily-rotation check live inside ``write``; callers may invoke it as
often as they like.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from typing import Optional

log = logging.getLogger('agv.frames')

_FILE_PREFIX = 'agv-frames-'
_FILE_SUFFIX = '.jsonl'
_MIN_INTERVAL_S = 0.05  # never emit more than 20 Hz regardless of hz knob


def _today_path(base_dir: str, today: Optional[date] = None) -> str:
    d = today or date.today()
    return os.path.join(base_dir, f'{_FILE_PREFIX}{d.isoformat()}{_FILE_SUFFIX}')


class FrameLog:
    def __init__(self, base_dir: str, hz: float = 5.0) -> None:
        self.base_dir = base_dir
        self.hz = max(0.1, float(hz))
        self._min_interval = max(_MIN_INTERVAL_S, 1.0 / self.hz)
        self._lock = threading.Lock()
        self._fh = None
        self._path: Optional[str] = None
        self._date: Optional[date] = None
        self._last_mono: float = 0.0
        self._writes: int = 0
        self._drops: int = 0

    @property
    def current_path(self) -> str:
        return self._path or ''

    def start(self) -> None:
        with self._lock:
            if self._fh is not None:
                return
            self._last_mono = 0.0              # reset throttle so the first write emits
            self._open_locked(date.today())

    def write(self, row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        now = time.monotonic()
        with self._lock:
            if self._fh is None:
                return False
            if (now - self._last_mono) < self._min_interval:
                self._drops += 1
                return False
            today = date.today()
            if today != self._date:
                self._rotate_locked(today)
            try:
                self._fh.write(json.dumps(row, separators=(',', ':')) + '\n')
                self._fh.flush()
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._last_mono = now
                self._writes += 1
                return True
            except OSError as exc:
                log.warning('[FrameLog] write failed: %s', exc)
                return False

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _open_locked(self, today: date) -> None:
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except OSError as exc:
            log.warning('[FrameLog] mkdir failed: %s', exc)
            return
        path = _today_path(self.base_dir, today)
        try:
            self._fh = open(path, 'a', buffering=1, encoding='utf-8')
            self._path = path
            self._date = today
        except OSError as exc:
            log.warning('[FrameLog] open %s failed: %s', path, exc)
            self._fh = None
            self._path = None
            self._date = None

    def _rotate_locked(self, today: date) -> None:
        self._close_locked()
        self._open_locked(today)

    def _close_locked(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
        except OSError as exc:
            log.warning('[FrameLog] close failed: %s', exc)
        finally:
            self._fh = None
            self._path = None
            self._date = None