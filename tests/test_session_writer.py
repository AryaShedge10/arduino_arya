"""SessionWriter: shards, ring, drops, crash recovery, events/telemetry, disk limit."""
import _env  # noqa: F401  (must be first)
import json
import os
import tempfile
import time
from unittest import mock

import numpy as np

import recording_io
import rec_store
import recorder_v1
from fixtures import synthetic_frame
from recorder_v1 import SessionWriter, build_meta

FRAME = synthetic_frame(0, 320, 180)


def _meta(name='t'):
    return build_meta(session=name, started=time.time(), started_mono=time.monotonic(),
                      mode='continuous', rate_hz=5, capture={'shard_s': 1},
                      image={'format': 'jpeg', 'quality': 88, 'frame_w': 320, 'frame_h': 180})


def _writer(name='t', **kw):
    root = tempfile.mkdtemp(prefix='sw-', dir=_env.TMP)
    kw.setdefault('min_free_gb', 0.0)
    w = SessionWriter(root, name, _meta(name), **kw)
    return w, os.path.join(root, name)


def _item(i, kind='continuous', t=None, flush=False, **row):
    r = {'i': i, 't': 1000.0 + i}
    if t is not None:
        r['t_mono'] = t
    r.update(row)
    return {'op': 'frame', 'kind': kind, 'i': i, 'frame': FRAME, 'row': r, 'flush_ring': flush}


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_continuous_shards():
    w, d = _writer(shard_s=1.0, fsync_s=0.2)
    w.start()
    for i in range(12):
        assert w.submit(_item(i, mark='x' if i == 3 else None, problem='line_lost' if i == 5 else ''))
        time.sleep(0.25)
    w.finish('stopped')
    w.join(10)
    assert w.finished.is_set() and not w.is_alive()
    top = json.load(open(os.path.join(d, 'manifest.json')))
    assert top['complete'] is True and len(top['shards']) >= 2
    for s in top['shards']:
        mp = os.path.join(d, 'shards', s['name'], 'manifest.json')
        assert rec_store.sha256_file(mp) == s['manifest_sha256']
        m = json.load(open(mp))
        for rel, info in m['files'].items():
            p = os.path.join(d, 'shards', s['name'], rel)
            assert rec_store.sha256_file(p) == info['sha256'], rel
    summ = json.load(open(os.path.join(d, 'summary.json')))
    assert summ['frames'] == 12 and summ['rows'] == 12 and summ['complete'] is True
    assert summ['marks'] == 1 and summ['problems'] == 1 and summ['stop_reason'] == 'stopped'
    assert summ['saved_by'] == {'continuous': 12}
    rows = recording_io.load_rows(d)
    assert [r['i'] for r in rows] == list(range(12))
    r = rows[0]
    assert r['save'] == 'continuous' and r['img']['w'] == 320 and 0 <= r['luma'] <= 255
    assert os.path.isfile(os.path.join(d, r['file']))
    meta = json.load(open(os.path.join(d, 'meta.json')))
    assert meta['schema'] == 1 and meta['format'] == 'agv-rec'
    for k in ('code', 'camera', 'geometry', 'robot', 'config', 'triggers', 'env', 'capture'):
        assert k in meta, k
    assert meta['geometry']['vision_height'] == 360
    assert len(meta['geometry']['homography_vision_to_bev']) == 3
    assert meta['camera']['exposure_range_ns'][0] == 13000
    assert 'src_fingerprint' in meta['code']


def test_ring_flush():
    w, d = _writer(pre_s=5.0)
    w.start()
    for k in range(20):
        assert w.submit(_item(k, 'ring', t=float(k)))
    assert w.submit(_item(100, 'trigger', t=20.0, flush=True, triggers=['line_lost'],
                          problem='line_lost'))
    assert _wait(lambda: w.rows >= 6)
    w.finish('stopped')
    w.join(10)
    rows = recording_io.load_rows(d, sort=False)
    saves = [r['save'] for r in rows]
    pre = [r for r in rows if r['save'] == 'pre']
    assert 4 <= len(pre) <= 6, len(pre)
    assert [r['i'] for r in pre] == sorted(r['i'] for r in pre)
    assert pre[-1]['i'] == 19 and saves[-1] == 'trigger' and rows[-1]['i'] == 100
    assert w.saved_by.get('trigger') == 1
    summ = json.load(open(os.path.join(d, 'summary.json')))
    assert summ['triggers'] == {'line_lost': 1}


def test_full_queue_drops():
    w, d = _writer(queue_max=1)
    assert w.submit(_item(0)) is True          # thread not started yet
    t0 = time.time()
    assert w.submit(_item(1)) is False
    assert w.submit(_item(2)) is False
    assert time.time() - t0 < 0.2
    assert w.dropped == 2
    w.start()
    w.finish('stopped')
    w.join(10)
    assert w.frames_saved == 1
    assert json.load(open(os.path.join(d, 'summary.json')))['dropped'] == 2
    # slowed encode with a running thread
    w2, _ = _writer(queue_max=1)
    orig = w2._encode

    def slow(frame, row):
        time.sleep(0.3)
        return orig(frame, row)
    w2._encode = slow
    w2.start()
    t0 = time.time()
    res = [w2.submit(_item(k)) for k in range(10)]
    assert time.time() - t0 < 0.2
    assert not all(res) and w2.dropped >= 1
    w2.finish('x')
    w2.join(10)


def test_crash_recovery():
    w, d = _writer()
    w.start()
    for i in range(5):
        w.submit(_item(i))
    assert _wait(lambda: w.rows == 5)
    w._crash_for_test()
    w.join(5)
    assert not w.is_alive() and not w.finished.is_set()
    assert not os.path.exists(os.path.join(d, 'summary.json'))
    top = json.load(open(os.path.join(d, 'manifest.json'))) if os.path.exists(
        os.path.join(d, 'manifest.json')) else {'complete': False}
    assert top['complete'] is False
    assert not os.path.exists(os.path.join(d, 'shards', 's0000', 'manifest.json'))
    rows = recording_io.load_rows(d)
    assert [r['i'] for r in rows] == list(range(5))


def test_events_and_telemetry():
    w, d = _writer()
    w.start()
    w.add_event({'t': 1.0, 'dt': 0.5, 'i': 3, 'kind': 'mark', 'text': 'hello', 't_mono': 9.0,
                 'junk': 1})
    for k in range(5):
        w.add_telem({'t': 2.0 + k, 't_mono': 10.0 + k, 'seq': k})
    assert _wait(lambda: w._telem_rows == 5 and w._events_n >= 2)
    w.finish('stopped')
    w.join(10)
    sd = os.path.join(d, 'shards', 's0000')
    ev = [json.loads(x) for x in open(os.path.join(sd, 'events.jsonl'))]
    kinds = [e['kind'] for e in ev]
    assert 'shard_open' in kinds and 'shard_close' not in kinds
    m = [e for e in ev if e['kind'] == 'mark'][0]
    assert m['text'] == 'hello' and m['shard'] == 0 and m['t_mono'] == 9.0 and 'junk' not in m
    tl = [json.loads(x) for x in open(os.path.join(sd, 'telemetry.jsonl'))]
    assert len(tl) == 5 and tl[4]['seq'] == 4
    assert any(e['kind'] == 'mark' for e in recording_io.load_events(d))
    assert json.load(open(os.path.join(d, 'summary.json')))['telemetry_rows'] == 5


def test_disk_limit_ends_by_itself():
    w, d = _writer(min_free_gb=1e9, disk_check_s=0.05)
    w.start()
    w.submit(_item(0))
    assert w.finished.wait(10)
    w.join(5)
    assert w.ended_reason == 'disk full'
    summ = json.load(open(os.path.join(d, 'summary.json')))
    assert summ['stop_reason'] == 'disk full' and summ['complete'] is True
    assert w.submit(_item(1)) is False or True  # must not raise


def test_save_width_and_size_limit():
    w, d = _writer(save_width=160, max_bytes=1, disk_check_s=0.05)
    w.start()
    w.submit(_item(0))
    assert w.finished.wait(10)
    assert w.ended_reason == 'size limit'
    rows = recording_io.load_rows(d)
    assert rows and rows[0]['img']['w'] == 160 and rows[0]['img']['h'] == 90


def test_build_meta_never_raises():
    m = build_meta(session='x', started='bad', started_mono=None, mode='m', rate_hz=1,
                   capture=None, image=None, extra={'foo': 1})
    assert m['schema'] == 1 and m['foo'] == 1
    with mock.patch.object(recorder_v1, '_config_snapshot', side_effect=RuntimeError):
        m = build_meta(session='x', started=1.0, started_mono=0.5, mode='m', rate_hz=1,
                       capture={}, image={})
        assert m['schema'] == 1 and m['session'] == 'x'


if __name__ == '__main__':
    _env.run_tests(globals())
