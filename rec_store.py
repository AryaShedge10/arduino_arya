"""Crash-safe storage primitives for recorder v1: atomic writes, shards, manifests.

No cv2/numpy here and no dependency on recorder.py.
"""
import hashlib
import json
import os
import time

import config

SCHEMA = 1
_JSONL = ('log.jsonl', 'events.jsonl', 'telemetry.jsonl')


def fsync_dir(path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path, data: bytes, fsync: bool = False) -> None:
    path = str(path)
    folder = os.path.dirname(path) or '.'
    tmp = os.path.join(folder, '.' + os.path.basename(path) + '.tmp')
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if fsync:
            fsync_dir(folder)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=1).encode('utf-8'), fsync=True)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _span(span, *vals):
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if span[0] is None or v < span[0]:
                span[0] = v
            if span[1] is None or v > span[1]:
                span[1] = v


class ShardWriter:
    def __init__(self, session_dir, index: int):
        self.session_dir = str(session_dir)
        self.index = int(index)
        self.name = 's%04d' % self.index
        self.dir = os.path.join(self.session_dir, 'shards', self.name)
        self.frames_dir = os.path.join(self.dir, 'frames')
        os.makedirs(self.frames_dir, exist_ok=True)
        self.dropped = 0
        self.closed = False
        self._opened_mono = time.monotonic()
        self._opened_wall = time.time()
        self._files = {n: open(os.path.join(self.dir, n), 'ab') for n in _JSONL}
        self._counts = {n: 0 for n in _JSONL}
        self._frames = {}          # rel-to-shard path -> (sha, nbytes)
        self._first_i = None
        self._last_i = None
        self._tm = [None, None]
        self._tw = [None, None]
        self._last_fsync = time.monotonic()

    # ---- properties
    @property
    def age_s(self) -> float:
        return time.monotonic() - self._opened_mono

    @property
    def frames(self) -> int:
        return len(self._frames)

    # ---- writers
    def write_frame(self, i: int, jpeg_bytes: bytes):
        fname = '%06d.jpg' % i
        tmp = os.path.join(self.frames_dir, '.' + fname + '.tmp')
        final = os.path.join(self.frames_dir, fname)
        try:
            with open(tmp, 'wb') as f:
                f.write(jpeg_bytes)
            os.replace(tmp, final)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        sha = hashlib.sha256(jpeg_bytes).hexdigest()
        n = len(jpeg_bytes)
        self._frames['frames/' + fname] = (sha, n)
        if self._first_i is None or i < self._first_i:
            self._first_i = i
        if self._last_i is None or i > self._last_i:
            self._last_i = i
        return 'shards/%s/frames/%s' % (self.name, fname), sha, n

    def _append(self, fname, d):
        f = self._files[fname]
        f.write((json.dumps(d, separators=(',', ':')) + '\n').encode('utf-8'))
        f.flush()
        self._counts[fname] += 1
        _span(self._tm, d.get('t_mono'))
        _span(self._tw, d.get('t'), d.get('t_wall'))

    def append_row(self, d):
        self._append('log.jsonl', d)

    def append_event(self, d):
        self._append('events.jsonl', d)

    def append_telem(self, d):
        self._append('telemetry.jsonl', d)

    def maybe_fsync(self, t_mono=None) -> bool:
        now = time.monotonic() if t_mono is None else t_mono
        if now - self._last_fsync < config.REC_FSYNC_S:
            return False
        self._last_fsync = now
        for f in self._files.values():
            f.flush()
            os.fsync(f.fileno())
        return True

    # ---- close
    def close(self) -> dict:
        if not self.closed:
            for f in self._files.values():
                try:
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    f.close()
            fsync_dir(self.frames_dir)
            self.closed = True
        files = {rel: {'sha256': s, 'bytes': n} for rel, (s, n) in self._frames.items()}
        for n in _JSONL:
            p = os.path.join(self.dir, n)
            files[n] = {'sha256': sha256_file(p), 'bytes': os.path.getsize(p)}
        tm = list(self._tm) if self._tm[0] is not None else [self._opened_mono] * 2
        tw = list(self._tw) if self._tw[0] is not None else [self._opened_wall] * 2
        man = {
            'schema': SCHEMA, 'shard': self.name, 'closed': True,
            'frames': {'count': len(self._frames), 'first_i': self._first_i,
                       'last_i': self._last_i},
            'rows': self._counts['log.jsonl'],
            'events': self._counts['events.jsonl'],
            'telemetry_rows': self._counts['telemetry.jsonl'],
            't_mono': tm, 't_wall': tw,
            'bytes': sum(v['bytes'] for v in files.values()),
            'dropped': int(self.dropped),
            'files': files,
        }
        atomic_write_json(os.path.join(self.dir, 'manifest.json'), man)
        fsync_dir(self.dir)
        return man


def write_session_manifest(session_dir, shard_manifests, complete: bool) -> dict:
    session_dir = str(session_dir)
    shards = []
    tot = {'frames': 0, 'rows': 0, 'events': 0, 'telemetry_rows': 0, 'bytes': 0, 'dropped': 0}
    for m in shard_manifests:
        mp = os.path.join(session_dir, 'shards', m['shard'], 'manifest.json')
        try:
            msha = sha256_file(mp)
        except OSError:
            msha = None
        fr = m['frames']['count']
        shards.append({'name': m['shard'], 'closed': bool(m.get('closed')), 'frames': fr,
                       'rows': m['rows'], 'bytes': m['bytes'], 'manifest_sha256': msha})
        tot['frames'] += fr
        tot['rows'] += m['rows']
        tot['events'] += m.get('events', 0)
        tot['telemetry_rows'] += m.get('telemetry_rows', 0)
        tot['bytes'] += m['bytes']
        tot['dropped'] += m.get('dropped', 0)
    top = {'schema': SCHEMA, 'session': os.path.basename(session_dir.rstrip('/')),
           'complete': bool(complete), 'updated_wall': time.time(),
           'shards': shards, 'totals': tot}
    atomic_write_json(os.path.join(session_dir, 'manifest.json'), top)
    return top
