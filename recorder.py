"""LEGACY (v0) test recorder: raw camera frames + a per-frame detection log.

This is the rollback recorder, selected with ``AGV_REC_FORMAT=v0``. The default
recorder (v1: shards, ring buffer / triggered mode, checksums) lives in
``recorder_v1.py``; ``make_recorder()`` at the bottom of this file picks one.

Started from the dashboard's Record tab. Each session is a folder under
``REC_DIR`` (default ``recordings/``):

    meta.json      when / how it was recorded + a snapshot of the tuning config
    frames/NNNNNN.jpg   the raw camera frames (what the detector really saw)
    views/NNNNNN.jpg    the detector's own views (top-down view, yellow mask,
                        obstacle test, ...) for problem / marked frames, or for
                        every frame when "views" is on
    log.jsonl      one JSON row per saved frame: line geometry, obstacle, the
                   decision, speeds, wheel PWM / encoder speeds, and the
                   pixel position of the detected line (for the review page)
    events.jsonl   mode changes, marks, automatic problem flags, stop reason
    summary.json   written when the session ends

Frames are saved at ``rate`` per second. Frames where something looks wrong
(line lost while driving, tracking STUCK / OFF_LINE, obstacle flagged) are
always saved, with the detector views, so they can be reviewed later. The
operator can also press "Mark wrong detection" for any frame.

JPEG encoding and disk writes happen on a worker thread; if the disk cannot
keep up, frames are dropped (and counted) rather than slowing the control loop.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

import config
from config import (
    REC_DIR, REC_JPEG_QUALITY, REC_MAX_GB, REC_MAX_MIN, REC_MIN_FREE_GB,
)

log = logging.getLogger('agv.recorder')

PROBLEM_MIN_GAP_S = 1.0          # at most one automatic problem save per second


def _config_snapshot() -> dict:
    """Plain-valued upper-case settings (thresholds, geometry, speeds...)."""
    out = {}
    for name in dir(config):
        if not name.isupper() or name.startswith('_'):
            continue
        val = getattr(config, name)
        if isinstance(val, (int, float, str, bool)) or val is None:
            out[name] = val
    return out


def free_gb(path: str = REC_DIR) -> float:
    probe = path if os.path.isdir(path) else os.path.dirname(path) or '.'
    try:
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return 0.0


class Recorder:
    def __init__(self, root: str = REC_DIR) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._q: 'queue.Queue' = queue.Queue(maxsize=48)
        self._worker: Optional[threading.Thread] = None
        self.active = False
        self._dir: Optional[str] = None
        self._name = ''
        self._t0 = 0.0
        self._last_save = 0.0
        self._last_problem = 0.0
        self._rate = 5.0
        self._views = False
        self._idx = 0
        self._frames = 0
        self._problems = 0
        self._marks = 0
        self._dropped = 0
        self._bytes = 0
        self._pending_mark: Optional[str] = None
        self._stop_reason = ''
        self._log_fh = None
        self._ev_fh = None

    # ── control ──────────────────────────────────────────────────────
    def start(self, rate_hz: float, views: bool, extra_meta: Optional[dict] = None,
              mode: str = 'continuous') -> str:   # mode: accepted for API parity, ignored
        with self._lock:
            if self.active:
                return self._name
            os.makedirs(self.root, exist_ok=True)
            if free_gb(self.root) < REC_MIN_FREE_GB:
                self._stop_reason = f'not enough free disk (< {REC_MIN_FREE_GB:g} GB)'
                log.error('[Recorder] %s', self._stop_reason)
                return ''
            self._name = time.strftime('%Y%m%d_%H%M%S')
            self._dir = os.path.join(self.root, self._name)
            os.makedirs(os.path.join(self._dir, 'frames'), exist_ok=True)
            os.makedirs(os.path.join(self._dir, 'views'), exist_ok=True)
            self._rate, self._views = max(0.2, float(rate_hz)), bool(views)
            self._t0 = time.time()
            self._last_save = self._last_problem = 0.0
            self._idx = self._frames = self._problems = self._marks = 0
            self._dropped = self._bytes = 0
            self._pending_mark = None
            self._stop_reason = ''
            meta = {'session': self._name, 'started': self._t0,
                    'started_iso': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'rate_hz': self._rate, 'views_every_frame': self._views,
                    'config': _config_snapshot()}
            meta.update(extra_meta or {})
            with open(os.path.join(self._dir, 'meta.json'), 'w', encoding='utf-8') as fh:
                json.dump(meta, fh, indent=1, default=str)
            self._log_fh = open(os.path.join(self._dir, 'log.jsonl'), 'a', encoding='utf-8')
            self._ev_fh = open(os.path.join(self._dir, 'events.jsonl'), 'a', encoding='utf-8')
            self.active = True
            self._worker = threading.Thread(target=self._run, name='recorder', daemon=True)
            self._worker.start()
        self.event('record_start', f'recording at {self._rate:g} fps')
        log.info('[Recorder] started %s (%.1f fps)', self._name, self._rate)
        return self._name

    def stop(self, reason: str = 'stopped from dashboard') -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
            self._stop_reason = reason
        self.event('record_stop', reason)
        self._q.put(None)
        if self._worker:
            self._worker.join(timeout=5.0)
        with self._lock:
            for fh in (self._log_fh, self._ev_fh):
                if fh:
                    fh.close()
            self._log_fh = self._ev_fh = None
            summary = {'session': self._name, 'frames': self._frames, 'problems': self._problems,
                       'marks': self._marks, 'dropped': self._dropped,
                       'size_mb': round(self._bytes / 1e6, 1),
                       'duration_s': round(time.time() - self._t0, 1), 'stop_reason': reason}
            try:
                with open(os.path.join(self._dir, 'summary.json'), 'w', encoding='utf-8') as fh:
                    json.dump(summary, fh)
            except OSError:
                pass
        log.info('[Recorder] stopped %s: %s', self._name, summary)

    def mark(self, note: str = '') -> None:
        """Flag the next frame as "wrong detection" (saved with detector views)."""
        if self.active:
            self._pending_mark = note or 'marked'

    def event(self, kind: str, text: str = '') -> None:
        if not self._ev_fh:
            return
        try:
            self._ev_fh.write(json.dumps({'t': time.time(), 'dt': round(time.time() - self._t0, 2),
                                          'i': self._idx, 'kind': kind, 'text': text}) + '\n')
            self._ev_fh.flush()
        except (OSError, ValueError):
            pass

    def close(self, timeout: float = 0) -> None:
        """API parity with RecorderV1 (v0 has nothing to wait for)."""

    # ── per control-loop tick ────────────────────────────────────────
    def tick(self, now_wall: float, frame: Optional[np.ndarray], row: dict,
             problem: str, views_fn: Callable[[], Optional[np.ndarray]], **_kw) -> None:
        """Maybe save this frame. ``row`` is the detection log row, ``problem``
        is '' or a short reason, ``views_fn`` returns the detector views."""
        if not self.active or frame is None:
            return
        if (now_wall - self._t0) > REC_MAX_MIN * 60:
            return self.stop(f'time limit ({REC_MAX_MIN:g} min)')
        if self._bytes > REC_MAX_GB * 1e9:
            return self.stop(f'size limit ({REC_MAX_GB:g} GB)')
        if self._frames % 50 == 0 and free_gb(self.root) < REC_MIN_FREE_GB:
            return self.stop(f'disk almost full (< {REC_MIN_FREE_GB:g} GB free)')
        mark, self._pending_mark = self._pending_mark, None
        due = (now_wall - self._last_save) >= 1.0 / self._rate
        auto = bool(problem) and (now_wall - self._last_problem) >= PROBLEM_MIN_GAP_S
        if not (due or auto or mark):
            return
        if due:
            self._last_save = now_wall
        i = self._idx
        self._idx += 1
        row = dict(row)
        row.update(i=i, t=round(now_wall, 3), dt=round(now_wall - self._t0, 3),
                   file=f'frames/{i:06d}.jpg')
        save_views = self._views or bool(mark) or auto
        if auto:
            self._last_problem = now_wall
            self._problems += 1
            row['problem'] = problem
            self.event('problem', problem)
        if mark:
            self._marks += 1
            row['mark'] = mark
            self.event('mark', mark)
        views = views_fn() if save_views else None
        if views is not None:
            row['views'] = f'views/{i:06d}.jpg'
        try:
            self._q.put_nowait((i, frame.copy(), views, row))
        except queue.Full:
            self._dropped += 1
            return
        self._frames += 1

    # ── status ───────────────────────────────────────────────────────
    def status(self) -> dict:
        return {'on': self.active, 'session': self._name, 'frames': self._frames,
                'problems': self._problems, 'marks': self._marks,
                'dropped': self._dropped, 'mb': round(self._bytes / 1e6, 1),
                'elapsed_s': round(time.time() - self._t0, 1) if self.active else 0.0,
                'free_gb': round(free_gb(self.root if os.path.isdir(self.root) else os.path.dirname(self.root) or '.'), 1),
                'rate_hz': self._rate, 'stop_reason': self._stop_reason}

    # ── worker ───────────────────────────────────────────────────────
    def _run(self) -> None:
        params = [cv2.IMWRITE_JPEG_QUALITY, REC_JPEG_QUALITY]
        while True:
            item = self._q.get()
            if item is None:
                return
            i, frame, views, row = item
            try:
                ok, buf = cv2.imencode('.jpg', frame, params)
                if ok:
                    with open(os.path.join(self._dir, row['file']), 'wb') as fh:
                        fh.write(buf.tobytes())
                    self._bytes += len(buf)
                if views is not None:
                    ok, vb = cv2.imencode('.jpg', views, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        with open(os.path.join(self._dir, row['views']), 'wb') as fh:
                            fh.write(vb.tobytes())
                        self._bytes += len(vb)
                if self._log_fh:
                    self._log_fh.write(json.dumps(row, default=str) + '\n')
                    self._log_fh.flush()
            except (OSError, ValueError) as exc:
                log.warning('[Recorder] write failed: %s', exc)


def make_recorder(root=None):
    """Recorder facade: RecorderV1 when AGV_REC_FORMAT=v1, else the v0 Recorder."""
    if config.REC_FORMAT == 'v1':
        from recorder_v1 import RecorderV1       # lazy: v0 never imports it
        return RecorderV1(root) if root else RecorderV1()
    return Recorder(root) if root else Recorder()
