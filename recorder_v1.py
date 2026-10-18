"""Recorder v1 building blocks: ``build_meta`` and the ``SessionWriter`` thread.

``SessionWriter`` is the single thread doing ALL disk work of one recording
session (folders, meta, JPEG encode, ring buffer, shards, fsync, manifests,
free-disk check, summary). The control side only calls the non-blocking
``submit`` / ``add_event`` / ``add_telem`` / ``finish``.

Queue item protocol (dicts)::

    {'op': 'frame', 'kind': 'continuous'|'baseline'|'trigger'|'mark'|'post'|'ring',
     'i': int, 'frame': ndarray(BGR), 'row': dict, 'flush_ring': bool}

The writer fills ``file, shard, save, img{w,h,bytes}, luma`` in the row.
"""
from __future__ import annotations

import collections
import logging
import os
import platform
import queue
import shutil
import socket
import threading
import time

import cv2
import numpy as np

import config
import rec_store
from build_info import get_build_info
from ringbuf import RingBuffer

log = logging.getLogger('agv.recorder')

SCHEMA = 1
RECORDER_VERSION = '1.0'
MAX_WRITE_ERRORS = 20


def _config_snapshot() -> dict:
    out = {}
    for name in dir(config):
        if not name.isupper() or name.startswith('_'):
            continue
        val = getattr(config, name)
        if isinstance(val, (int, float, str, bool)) or val is None:
            out[name] = val
    return out


def _g(name, default=None):
    return getattr(config, name, default)


def build_meta(*, session, started, started_mono, mode, rate_hz, capture, image,
               camera_extra=None, firmware=None, extra=None) -> dict:
    """meta.json v1. Pure; never raises."""
    try:
        return _build_meta(session, started, started_mono, mode, rate_hz, capture,
                           image, camera_extra, firmware, extra)
    except Exception:
        log.exception('build_meta failed')
        return {'schema': SCHEMA, 'format': 'agv-rec',
                'recorder_version': RECORDER_VERSION, 'session': session,
                'started': started, 'mode': mode, 'rate_hz': rate_hz,
                'views_every_frame': False}


def _build_meta(session, started, started_mono, mode, rate_hz, capture, image,
                camera_extra, firmware, extra) -> dict:
    image = dict(image or {})
    try:
        iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started))
    except Exception:
        iso = None
    try:
        wmm = round(float(started) - float(started_mono), 6)
    except Exception:
        wmm = None

    backend = _g('CAMERA_BACKEND')
    camera = {
        'backend': backend, 'sensor_id': _g('CAMERA_SENSOR_ID'),
        'width': _g('CAMERA_WIDTH'), 'height': _g('CAMERA_HEIGHT'),
        'fps': _g('CAMERA_FPS'), 'wb_mode': _g('CAMERA_WB_MODE'),
        'wb_locked': _g('CAMERA_WB_LOCK'),
        'exposure_range_ns': [13000, _g('CAMERA_EXPOSURE_MAX_NS')],
        'exposure_auto': True, 'gain': 'auto', 'per_frame_exposure': None,
        'flip': _g('CAMERA_FLIP'),
        'green_cast_ratio': _g('CAMERA_GREEN_CAST_MAX_RATIO'),
        'pipeline': backend,
    }
    if camera_extra:
        camera.update(camera_extra)

    vw = _g('VISION_WIDTH')
    vh = None
    fw, fh = image.get('frame_w'), image.get('frame_h')
    try:
        if fw and fh:
            vh = int(round(fh * vw / fw))
    except Exception:
        vh = None
    homog = None
    try:
        if vw and vh:
            import vision  # lazy: heavy import
            homog = np.asarray(vision.build_homography(vw, vh), dtype=float).tolist()
    except Exception:
        homog = None
    geometry = {
        'cam_height_m': _g('CAM_HEIGHT_M'), 'cam_tilt_deg': _g('CAM_TILT_DEG'),
        'cam_hfov_deg': _g('CAM_HFOV_DEG'), 'vision_width': vw, 'vision_height': vh,
        'bev': {'x_half_m': _g('BEV_X_HALF_M'), 'y_near_m': _g('BEV_Y_NEAR_M'),
                'y_far_m': _g('BEV_Y_FAR_M'), 'px_per_m': _g('BEV_PX_PER_M')},
        'homography_vision_to_bev': homog,
    }
    try:
        host = socket.gethostname()
    except Exception:
        host = None
    robot = {
        'robot_id': _g('ROBOT_ID'), 'host': host,
        'wheel_diameter_m': _g('WHEEL_DIAMETER_M'), 'ticks_per_m': _g('TICKS_PER_M'),
        'enc_ppr': _g('ENC_PPR'), 'enc_gear': _g('ENC_GEAR'),
        'wheel_base_m': _g('WHEEL_BASE_M'), 'rover_width_m': _g('ROVER_WIDTH_M'),
        'arduino_port': _g('ARDUINO_PORT'),
    }
    bi = get_build_info() or {}
    code = {k: bi.get(k) for k in ('git_commit', 'git_dirty', 'git_branch',
                                   'src_sha256', 'src_fingerprint')}
    triggers = {
        'cooldown_s': _g('REC_TRIG_COOLDOWN_S'), 'max_offset_m': _g('REC_TRIG_MAX_OFFSET_M'),
        'max_angle_deg': _g('REC_TRIG_MAX_ANGLE_DEG'), 'min_strips': _g('REC_TRIG_MIN_STRIPS'),
        'jump_m': _g('REC_TRIG_JUMP_M'), 'jump_deg': _g('REC_TRIG_JUMP_DEG'),
        'persist_s': _g('REC_TRIG_PERSIST_S'), 'rearm_s': _g('REC_TRIG_REARM_S'),
    }
    env = {'python': platform.python_version(), 'opencv': getattr(cv2, '__version__', None)}
    meta = {
        'schema': SCHEMA, 'format': 'agv-rec', 'recorder_version': RECORDER_VERSION,
        'session': session, 'started': started, 'started_iso': iso,
        'started_mono': started_mono, 'wall_minus_mono': wmm,
        'mode': mode, 'rate_hz': rate_hz, 'views_every_frame': False,
        'capture': dict(capture or {}), 'image': image, 'camera': camera,
        'geometry': geometry, 'robot': robot, 'firmware': dict(firmware or {}),
        'code': code, 'triggers': triggers, 'env': env,
        'config': _config_snapshot(),
    }
    if extra:
        meta.update(extra)
    return meta


class SessionWriter(threading.Thread):
    def __init__(self, root, name, meta, *, mode='continuous',
                 shard_s=config.REC_SHARD_S, shard_max_frames=config.REC_SHARD_MAX_FRAMES,
                 fsync_s=config.REC_FSYNC_S, queue_max=config.REC_QUEUE_MAX,
                 pre_s=config.REC_PRE_S, ring_max_mb=config.REC_RING_MAX_MB,
                 jpeg_quality=config.REC_JPEG_QUALITY, save_width=config.REC_SAVE_WIDTH,
                 max_bytes=config.REC_MAX_GB * 1e9, min_free_gb=config.REC_MIN_FREE_GB,
                 disk_check_s=10.0):
        super().__init__(name='agv-rec-writer', daemon=True)
        self.root = str(root)
        self.session = name
        self.session_dir = os.path.join(self.root, name)
        self.meta = meta
        self.mode = mode
        self.shard_s = float(shard_s)
        self.shard_max_frames = int(shard_max_frames)
        self.fsync_s = float(fsync_s)
        self.pre_s = float(pre_s)
        self.jpeg_quality = int(jpeg_quality)
        self.save_width = int(save_width or 0)
        self.max_bytes = float(max_bytes)
        self.min_free_gb = float(min_free_gb)
        self.disk_check_s = float(disk_check_s)

        self._q = queue.Queue(maxsize=max(1, int(queue_max)))
        self._events = collections.deque(maxlen=5000)
        self._telem = collections.deque(maxlen=4000)
        self._ring = RingBuffer(max(self.pre_s, 1e-3), max(int(ring_max_mb * 1e6), 1))
        self._stop_ev = threading.Event()
        self._stop_reason = 'stopped'
        self._crash = False

        # status (written by the thread, read anywhere)
        self.frames_saved = 0
        self.rows = 0
        self.bytes = 0
        self.dropped = 0
        self.errors = 0
        self.shard_index = 0
        self.ring_len = 0
        self.ring_s = 0.0
        self.ring_mb = 0.0
        self.saved_by = {}
        self.free_gb = None
        self.ended_reason = ''
        self.finished = threading.Event()

        self._shard = None
        self._manifests = []
        self._problems = 0
        self._marks = 0
        self._trig_counts = {}
        self._telem_rows = 0
        self._events_n = 0
        self._t0 = None
        self._last_fsync = 0.0
        self._last_disk = 0.0

    # ---- control-thread API (non-blocking)
    def submit(self, item) -> bool:
        if self._stop_ev.is_set():
            return False
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def add_event(self, d) -> None:
        self._events.append(d)

    def add_telem(self, d) -> None:
        self._telem.append(d)

    def finish(self, reason: str = 'stopped') -> None:
        if not self._stop_ev.is_set():
            self._stop_reason = reason
        self._stop_ev.set()

    def _crash_for_test(self) -> None:
        self._crash = True
        self._stop_ev.set()

    # ---- thread body
    def run(self) -> None:
        try:
            self._setup()
        except Exception:
            log.exception('session setup failed: %s', self.session_dir)
            self.ended_reason = 'setup failed'
            self.finished.set()
            return
        while True:
            if self._crash:
                return
            try:
                stopping = self._stop_ev.is_set()
                try:
                    item = self._q.get(timeout=0.0 if stopping else 0.5)
                except queue.Empty:
                    item = None
                if self._crash:
                    return
                if item is not None:
                    self._handle(item)
                self._housekeeping()
                if self.ended_reason:
                    break
                if stopping and item is None:
                    break
            except Exception:
                self._error('loop')
                if self.errors > MAX_WRITE_ERRORS:
                    self.ended_reason = 'write errors'
                    break
        self._finalise()

    def _setup(self) -> None:
        os.makedirs(self.session_dir, exist_ok=True)
        self._t0 = time.monotonic()
        self._last_fsync = self._last_disk = self._t0
        self.free_gb = self._free_gb()
        rec_store.atomic_write_json(os.path.join(self.session_dir, 'meta.json'), self.meta)
        self._open_shard(0)

    def _error(self, what) -> None:
        self.errors += 1
        log.exception('recorder writer error (%s) #%d', what, self.errors)

    def _free_gb(self):
        try:
            return shutil.disk_usage(self.session_dir).free / 1e9
        except OSError:
            return 0.0

    def _open_shard(self, index) -> None:
        self._shard = rec_store.ShardWriter(self.session_dir, index)
        self.shard_index = index
        self._shard.append_event({'t': time.time(), 'dt': self._dt(), 'i': None,
                                  'kind': 'shard_open', 'text': self._shard.name,
                                  't_mono': time.monotonic(), 'shard': index})
        self._events_n += 1

    def _dt(self):
        return round(time.monotonic() - self._t0, 3) if self._t0 else 0.0

    def _rotate(self) -> None:
        sh = self._shard
        sh.append_event({'t': time.time(), 'dt': self._dt(), 'i': None,
                         'kind': 'shard_close', 'text': sh.name,
                         't_mono': time.monotonic(), 'shard': self.shard_index})
        self._events_n += 1
        sh.dropped = self.dropped
        self._manifests.append(sh.close())
        self._shard = None
        rec_store.write_session_manifest(self.session_dir, self._manifests, complete=False)
        self._open_shard(self.shard_index + 1)

    # ---- items
    def _handle(self, item) -> None:
        if item.get('op') != 'frame':
            return
        kind = item.get('kind', 'continuous')
        i = int(item['i'])
        row = dict(item.get('row') or {})
        row.setdefault('i', i)
        jpeg, row = self._encode(item['frame'], row)
        if kind == 'ring':
            t = row.get('t_mono')
            if not isinstance(t, (int, float)):
                t = time.monotonic()
            self._ring.push(t, i, jpeg, row)
            self._ring_stats()
            return
        if item.get('flush_ring'):
            for (_t, pi, pj, prow) in self._ring.drain():
                self._write(pi, pj, prow, 'pre')
            self._ring_stats()
        self._write(i, jpeg, row, kind)

    def _ring_stats(self) -> None:
        self.ring_len = len(self._ring)
        self.ring_s = round(self._ring.span_s, 2)
        self.ring_mb = round(self._ring.nbytes / 1e6, 2)

    def _encode(self, frame, row):
        h, w = frame.shape[:2]
        if 0 < self.save_width < w:
            nh = max(1, int(round(h * self.save_width / w)))
            frame = cv2.resize(frame, (self.save_width, nh), interpolation=cv2.INTER_AREA)
            h, w = nh, self.save_width
        ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if not ok:
            raise RuntimeError('imencode failed')
        jpeg = buf.tobytes()
        small = cv2.resize(frame, (32, 18), interpolation=cv2.INTER_AREA)
        grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        row['luma'] = round(float(grey.mean()), 1)
        row['img'] = {'w': int(w), 'h': int(h), 'bytes': len(jpeg)}
        return jpeg, row

    def _write(self, i, jpeg, row, save) -> None:
        sh = self._shard
        rel, _sha, n = sh.write_frame(i, jpeg)
        row['file'] = rel
        row['shard'] = sh.index
        row['save'] = save
        sh.append_row(row)
        self.frames_saved += 1
        self.rows += 1
        self.bytes += n
        self.saved_by[save] = self.saved_by.get(save, 0) + 1
        if row.get('problem') or row.get('triggers'):
            self._problems += 1
        if row.get('mark'):
            self._marks += 1
        for t in (row.get('triggers') or ()):
            if isinstance(t, str):
                self._trig_counts[t] = self._trig_counts.get(t, 0) + 1
        if sh.age_s >= self.shard_s or sh.frames >= self.shard_max_frames:
            self._rotate()

    def _drain_deques(self) -> None:
        sh = self._shard
        if sh is None:
            return
        while True:
            try:
                e = self._events.popleft()
            except IndexError:
                break
            try:
                d = {k: e[k] for k in ('t', 'dt', 'i', 'kind', 'text', 't_mono') if k in e}
                d['shard'] = self.shard_index
                sh.append_event(d)
                self._events_n += 1
            except Exception:
                self._error('event')
        while True:
            try:
                r = self._telem.popleft()
            except IndexError:
                break
            try:
                sh.append_telem(r)
                self._telem_rows += 1
            except Exception:
                self._error('telem')

    def _housekeeping(self) -> None:
        self._drain_deques()
        now = time.monotonic()
        sh = self._shard
        if sh is not None:
            if now - self._last_fsync >= self.fsync_s:
                self._last_fsync = now
                sh.maybe_fsync(now + 1e9)
            if sh.age_s >= self.shard_s or sh.frames >= self.shard_max_frames:
                self._rotate()
        if now - self._last_disk >= self.disk_check_s:
            self._last_disk = now
            self.free_gb = self._free_gb()
            if self.free_gb < self.min_free_gb:
                self.ended_reason = 'disk full'
            elif self.bytes > self.max_bytes:
                self.ended_reason = 'size limit'
        if self.errors > MAX_WRITE_ERRORS and not self.ended_reason:
            self.ended_reason = 'write errors'

    # ---- finalise
    def _finalise(self) -> None:
        reason = self.ended_reason or self._stop_reason
        try:
            if self.ended_reason != 'write errors':
                while True:   # drain whatever is still queued
                    try:
                        item = self._q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle(item)
                    except Exception:
                        self._error('final item')
            self._drain_deques()
            if self._shard is not None:
                self._shard.dropped = self.dropped
                self._manifests.append(self._shard.close())
                self._shard = None
            top = rec_store.write_session_manifest(self.session_dir, self._manifests,
                                                   complete=True)
            tot = top['totals']
            dur = round(time.monotonic() - self._t0, 1)
            summary = {
                'session': self.session, 'frames': self.frames_saved,
                'problems': self._problems, 'marks': self._marks,
                'dropped': self.dropped, 'size_mb': round(tot['bytes'] / 1e6, 1),
                'duration_s': dur, 'stop_reason': reason,
                'schema': SCHEMA, 'mode': self.mode, 'shards': len(self._manifests),
                'rows': self.rows, 'complete': True, 'triggers': dict(self._trig_counts),
                'saved_by': dict(self.saved_by), 'telemetry_rows': self._telem_rows,
                'bytes': tot['bytes'],
            }
            rec_store.atomic_write_json(os.path.join(self.session_dir, 'summary.json'),
                                        summary)
        except Exception:
            self._error('finalise')
        finally:
            self.finished.set()


# ──────────────────────────────────────────────────────────────────────
# RecorderV1: control-thread front end (same public API as recorder.Recorder)
# ──────────────────────────────────────────────────────────────────────
_EPS = 1e-6   # tolerance for float jitter in 'due' comparisons


class RecorderV1:
    """Cheap, non-blocking front end for one-at-a-time ``SessionWriter`` sessions.

    Everything called from the control thread (start/stop/mark/event/tick/status)
    does no disk I/O (``start`` does a single ``os.path.exists`` stat), no JPEG
    encoding and never calls ``views_fn``.
    """

    FORMAT = 'v1'

    def __init__(self, root: str = config.REC_DIR, **overrides) -> None:
        self.root = str(root)
        o = dict(overrides)

        def opt(key, default):
            v = o.pop(key, None)
            return default if v is None else v

        self.shard_s = float(opt('shard_s', config.REC_SHARD_S))
        self.shard_max_frames = int(opt('shard_max_frames', config.REC_SHARD_MAX_FRAMES))
        self.fsync_s = float(opt('fsync_s', config.REC_FSYNC_S))
        self.queue_max = int(opt('queue_max', config.REC_QUEUE_MAX))
        self.pre_s = float(opt('pre_s', config.REC_PRE_S))
        self.ring_hz = max(0.1, float(opt('ring_hz', config.REC_RING_HZ)))
        self.ring_max_mb = float(opt('ring_max_mb', config.REC_RING_MAX_MB))
        self.post_s = float(opt('post_s', config.REC_POST_S))
        self.baseline_hz = max(0.01, float(opt('baseline_hz', config.REC_BASELINE_HZ)))
        self.telem_hz = max(0.1, float(opt('telem_hz', config.REC_TELEM_HZ)))
        self.jpeg_quality = int(opt('jpeg_quality', config.REC_JPEG_QUALITY))
        self.save_width = int(opt('save_width', config.REC_SAVE_WIDTH))
        self.max_gb = float(opt('max_gb', config.REC_MAX_GB))
        self.max_min = float(opt('max_min', config.REC_MAX_MIN))
        self.min_free_gb = float(opt('min_free_gb', config.REC_MIN_FREE_GB))
        self.problem_gap_s = float(opt('problem_gap_s', 1.0))
        if o:
            raise TypeError(f'unknown RecorderV1 option(s): {sorted(o)}')

        self._writers = []            # every writer ever started (close() joins them)
        self._writer = None           # current / last writer
        self._active = False
        self._name = ''
        self._last_name = ''
        self._used_names = set()                # every name handed out by this instance
        self._mode = 'continuous'
        self._rate = 5.0
        self._stop_reason = ''
        self._t0 = 0.0
        self._reset_counters()
        self._pending_mark = None

    def _reset_counters(self) -> None:
        self._idx = 0
        self._problems = 0
        self._marks = 0
        self._last_save = 0.0
        self._last_problem = 0.0
        self._last_baseline = float('-inf')
        self._last_ring = float('-inf')
        self._last_post = float('-inf')
        self._last_telem = float('-inf')
        self._post_end = float('-inf')
        self._last_trig = float('-inf')
        self._telem_rows = 0

    # ── active flag ──────────────────────────────────────────────────
    def _sync(self) -> None:
        """Notice a writer that ended by itself (disk full, size limit, errors)."""
        w = self._writer
        if self._active and w is not None and (w.ended_reason or w.finished.is_set()):
            self._active = False
            self._stop_reason = w.ended_reason or self._stop_reason or 'writer stopped'
            log.warning('[RecorderV1] writer ended by itself: %s', self._stop_reason)

    @property
    def active(self) -> bool:
        self._sync()
        return self._active

    # ── control ──────────────────────────────────────────────────────
    def _unique_name(self) -> str:
        sec = int(time.time())
        while True:
            name = time.strftime('%Y%m%d_%H%M%S', time.localtime(sec))
            # The writer thread creates the folder asynchronously, so the stat alone
            # cannot see a session that was started a moment ago: remember every
            # name this instance used (not only the last one).
            if (name != self._last_name and name not in self._used_names
                    and not os.path.exists(os.path.join(self.root, name))):
                self._used_names.add(name)
                return name
            sec += 1

    def start(self, rate_hz: float, views=None, extra_meta=None,
              mode: str = 'continuous') -> str:
        if self.active:
            return self._name
        try:
            extra = dict(extra_meta or {})
            image_x = dict(extra.pop('image', None) or {})
            camera_x = extra.pop('camera', None)
            firmware = extra.pop('firmware', None)
            mode = mode if mode in ('continuous', 'triggered') else 'continuous'
            rate = max(0.2, float(rate_hz))
            name = self._unique_name()
            t_wall, t_mono = time.time(), time.monotonic()
            capture = {
                'baseline_hz': self.baseline_hz, 'ring_hz': self.ring_hz,
                'pre_s': self.pre_s, 'post_s': self.post_s,
                'ring_max_mb': self.ring_max_mb, 'shard_s': self.shard_s,
                'shard_max_frames': self.shard_max_frames, 'telem_hz': self.telem_hz,
                'fsync_s': self.fsync_s,
            }
            image = {'format': 'jpeg', 'quality': self.jpeg_quality,
                     'save_width': self.save_width,
                     'frame_w': None, 'frame_h': None,
                     'post_flip': False, 'green_cast_fix': True}
            image.update(image_x)
            meta = build_meta(session=name, started=t_wall, started_mono=t_mono,
                              mode=mode, rate_hz=rate, capture=capture, image=image,
                              camera_extra=camera_x, firmware=firmware, extra=extra)
            writer = SessionWriter(
                self.root, name, meta, mode=mode, shard_s=self.shard_s,
                shard_max_frames=self.shard_max_frames, fsync_s=self.fsync_s,
                queue_max=self.queue_max, pre_s=self.pre_s,
                ring_max_mb=self.ring_max_mb, jpeg_quality=self.jpeg_quality,
                save_width=self.save_width, max_bytes=self.max_gb * 1e9,
                min_free_gb=self.min_free_gb)
            writer.start()
        except Exception:
            log.exception('[RecorderV1] start failed')
            return ''
        self._writers = [w for w in self._writers if w.is_alive()]
        self._writers.append(writer)
        self._writer = writer
        self._name = self._last_name = name
        self._mode = mode
        self._rate = rate
        self._t0 = t_wall
        self._stop_reason = ''
        self._reset_counters()
        self._pending_mark = None
        self._active = True
        self.event('record_start', f'{mode} recording at {rate:g} fps')
        log.info('[RecorderV1] started %s (%s, %.1f fps)', name, mode, rate)
        return name

    def stop(self, reason: str = 'stopped from dashboard') -> None:
        """Non-blocking: flags the writer to finish; never joins."""
        self._sync()
        w = self._writer
        if not self._active or w is None:
            return
        self._active = False
        self._stop_reason = reason
        self.event('record_stop', reason)
        w.finish(reason)
        log.info('[RecorderV1] stopping %s: %s', self._name, reason)

    def close(self, timeout: float = 5.0) -> None:
        """Wait (bounded by ``timeout`` in total) for all writers to finalise."""
        end = time.monotonic() + max(0.0, float(timeout))
        for w in list(self._writers):
            if w.is_alive():
                w.join(max(0.0, end - time.monotonic()))
        self._writers = [w for w in self._writers if w.is_alive()]

    def mark(self, note: str = '') -> None:
        """Flag the next frame as "wrong detection"."""
        if self._active:
            self._pending_mark = note or 'marked'

    def event(self, kind: str, text: str = '', *, t_mono=None) -> None:
        w = self._writer
        if w is None:
            return
        now = time.time()
        w.add_event({'t': now, 't_mono': time.monotonic() if t_mono is None else t_mono,
                     'dt': round(now - self._t0, 3), 'i': self._idx,
                     'kind': kind, 'text': text})

    def _crash_for_test(self) -> None:
        if self._writer is not None:
            self._writer._crash_for_test()

    # ── per control-loop tick ────────────────────────────────────────
    def tick(self, now_wall: float, frame, row: dict, problem: str, views_fn=None,
             *, t_mono=None, triggers=(), telem=None) -> None:
        """Decide whether to save this frame. Never calls ``views_fn``."""
        self._sync()
        if not self._active or frame is None:
            return
        w = self._writer
        if t_mono is None:
            t_mono = time.monotonic()
        if (now_wall - self._t0) > self.max_min * 60:
            return self.stop(f'time limit ({self.max_min:g} min)')

        if isinstance(telem, dict) and (t_mono - self._last_telem) >= 1.0 / self.telem_hz - _EPS:
            self._last_telem = t_mono
            w.add_telem(dict(telem, t=now_wall if telem.get('t') is None else telem['t'],
                             t_mono=t_mono))
            self._telem_rows += 1

        mark, self._pending_mark = self._pending_mark, None
        trig = []
        if self._mode == 'triggered':
            kind, flush = self._decide_triggered(t_mono, list(triggers or ()), mark, trig)
        else:
            kind, flush = self._decide_continuous(now_wall, problem, mark)
            trig = list(triggers or ())
        if kind is None:
            return

        i = self._idx
        self._idx += 1
        row = dict(row)
        row.update(i=i, t=round(now_wall, 3), dt=round(now_wall - self._t0, 3),
                   triggers=list(trig))
        if row.get('t_mono') is None:
            row['t_mono'] = t_mono
        if self._mode == 'triggered':
            prob = '+'.join(t for t in trig if t != 'mark')
        else:
            prob = problem or ''
        row.pop('problem', None)
        if prob:
            row['problem'] = prob
        if mark:
            row['mark'] = mark
        if w._q.full():          # skip the 0.4 ms copy when it would be dropped anyway
            w.dropped += 1
            return
        w.submit({'op': 'frame', 'kind': kind, 'i': i, 'frame': frame.copy(),
                  'row': row, 'flush_ring': flush})

    def _decide_continuous(self, now_wall, problem, mark):
        """v0 rules: periodic save, plus automatic problem saves (>= problem_gap_s
        apart) and operator marks."""
        due = (now_wall - self._last_save) >= 1.0 / self._rate - _EPS
        auto = bool(problem) and (now_wall - self._last_problem) >= self.problem_gap_s
        if not (due or auto or mark):
            return None, False
        if due:
            self._last_save = now_wall
        if auto:
            self._last_problem = now_wall
            self._problems += 1
            self.event('problem', problem)
        if mark:
            self._marks += 1
            self.event('mark', mark)
        return ('mark' if mark else 'trigger' if auto else 'continuous'), False

    def _decide_triggered(self, t_mono, trig, mark, trig_out):
        """Triggered mode. Priority: trigger/mark > post window > baseline > ring.
        Auto triggers closer than ``problem_gap_s`` to the last trigger save are
        not saved again (they only extend the post window); marks always save.
        ``trig_out`` receives the trigger names of a queued trigger/mark frame."""
        fresh = bool(trig) and (t_mono - self._last_trig) >= self.problem_gap_s
        if trig and not fresh:
            self._post_end = max(self._post_end, t_mono + self.post_s)
        if fresh or mark:
            names = list(trig) if fresh else []
            if mark:
                names.append('mark')
            trig_out.extend(names)
            self._last_trig = t_mono
            self._last_post = self._last_ring = t_mono
            self._post_end = max(self._post_end, t_mono + self.post_s)
            if fresh:
                self._problems += 1
                self.event('trigger', '+'.join(trig), t_mono=t_mono)
            if mark:
                self._marks += 1
                self.event('mark', mark, t_mono=t_mono)
            return ('mark' if mark else 'trigger'), True
        if t_mono <= self._post_end and (t_mono - self._last_post) >= 1.0 / self.ring_hz - _EPS:
            self._last_post = self._last_ring = t_mono
            return 'post', False
        if (t_mono - self._last_baseline) >= 1.0 / self.baseline_hz - _EPS:
            self._last_baseline = self._last_ring = t_mono
            return 'baseline', False
        if (t_mono - self._last_ring) >= 1.0 / self.ring_hz - _EPS:
            self._last_ring = t_mono
            return 'ring', False
        return None, False

    # ── status (no disk I/O) ─────────────────────────────────────────
    def status(self) -> dict:
        self._sync()
        w = self._writer
        free = getattr(w, 'free_gb', None) if w else None
        return {
            'on': self._active, 'session': self._name,
            'frames': w.frames_saved if w else 0,
            'problems': self._problems, 'marks': self._marks,
            'dropped': w.dropped if w else 0,
            'mb': round((w.bytes if w else 0) / 1e6, 1),
            'elapsed_s': round(time.time() - self._t0, 1) if self._active else 0.0,
            'free_gb': round(free, 1) if free is not None else 0.0,
            'rate_hz': self._rate, 'stop_reason': self._stop_reason,
            'format': 'v1', 'mode': self._mode,
            'shard': w.shard_index if w else 0,
            'ring_s': w.ring_s if w else 0.0, 'ring_mb': w.ring_mb if w else 0.0,
            'saved_by': dict(w.saved_by) if w else {},
            'telem_rows': self._telem_rows,
        }
