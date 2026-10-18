"""RecorderV1: decision logic, non-blocking behaviour, status, lifecycle."""
import _env  # noqa: F401  (must be first)
import glob
import json
import os
import tempfile
import time
from contextlib import contextmanager
from unittest import mock

import recording_io
from fixtures import synthetic_frame
from recorder_v1 import RecorderV1, SessionWriter

SMALL = synthetic_frame(0, 320, 180)


def _never_views():
    raise AssertionError('views_fn must never be called')


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@contextmanager
def recorder(**kw):
    root = tempfile.mkdtemp(prefix='rv1-', dir=_env.TMP)
    kw.setdefault('min_free_gb', 0.0)
    kw.setdefault('queue_max', 1000)
    rec = RecorderV1(root, **kw)
    try:
        yield rec, root
    finally:
        rec.close(10.0)


def _session(root, name):
    return os.path.join(root, name)


def _finish(rec, reason='done'):
    rec.stop(reason)
    rec.close(10.0)


def test_continuous_5hz():
    with recorder() as (rec, root):
        name = rec.start(5, None)
        assert name and rec.active
        base = time.time()
        for k in range(30):
            problem = 'line_lost' if k == 12 else ''
            rec.tick(base + k * 0.1, SMALL, {'seq': k}, problem, _never_views,
                     t_mono=100 + k * 0.1)
        _finish(rec)
        rows = recording_io.load_rows(_session(root, name))
        assert 14 <= len(rows) <= 17, len(rows)
        probs = [r for r in rows if r.get('problem') == 'line_lost']
        assert len(probs) == 1 and probs[0]['save'] == 'trigger'
        assert all(r['save'] in ('continuous', 'trigger') for r in rows)
        assert [r['i'] for r in rows] == sorted(r['i'] for r in rows)
        assert os.path.exists(os.path.join(_session(root, name), 'summary.json'))


def test_triggered_pre_post_baseline():
    with recorder(pre_s=8, ring_hz=10, baseline_hz=0.5, post_s=3, ring_max_mb=48) as (rec, root):
        name = rec.start(5, None, mode='triggered')
        base = time.time()
        for k in range(120):
            trig = ['line_lost'] if k == 100 else ()
            rec.tick(base + k * 0.1, SMALL, {'seq': k}, '', _never_views,
                     t_mono=500 + k * 0.1, triggers=trig)
        _finish(rec)
        d = _session(root, name)
        raw = recording_io.load_rows(d, sort=False)
        by = {}
        for r in raw:
            by.setdefault(r['save'], []).append(r)
        assert len(by.get('pre', [])) >= 30, {k: len(v) for k, v in by.items()}
        assert len(by['trigger']) == 1 and len(by['post']) >= 10
        assert len(by['baseline']) >= 4
        trig_pos = raw.index(by['trigger'][0])
        pre_pos = [raw.index(r) for r in by['pre']]
        assert max(pre_pos) < trig_pos
        pre_i = [r['i'] for r in by['pre']]
        assert pre_i == sorted(pre_i)
        assert by['trigger'][0]['problem'] == 'line_lost'
        assert by['trigger'][0]['triggers'] == ['line_lost']
        srt = recording_io.load_rows(d)
        assert [r['i'] for r in srt] == sorted(r['i'] for r in srt)
        ev = [e['kind'] for e in recording_io.load_events(d)]
        assert 'trigger' in ev and 'record_start' in ev and 'record_stop' in ev
        st = rec.status()
        assert st['mode'] == 'triggered' and st['saved_by'].get('pre', 0) >= 30


def test_triggered_persistent_condition_is_throttled():
    from triggers import TriggerEngine
    eng = TriggerEngine(persist_s=5.0)
    lost = {'line': {'found': False}}
    n_ticks = 300                                   # 30 s at 10 Hz
    with recorder(pre_s=1.0, ring_hz=5, baseline_hz=0.5, post_s=1.0, ring_max_mb=48) as (rec, root):
        name = rec.start(5, None, mode='triggered')
        base = time.time()
        fire_t = []
        for k in range(n_ticks):
            t = 500 + k * 0.1
            trig = eng.evaluate(lost, t)
            if trig:
                fire_t.append(k)
            rec.tick(base + k * 0.1, SMALL, {'seq': k}, '', _never_views, t_mono=t, triggers=trig)
        _finish(rec)
        raw = recording_io.load_rows(_session(root, name), sort=False)
    by = {}
    for r in raw:
        by.setdefault(r['save'], []).append(r)
    counts = {k: len(v) for k, v in by.items()}
    assert 5 <= len(fire_t) <= 7, fire_t
    assert len(raw) < 0.4 * n_ticks, (len(raw), counts)
    assert len(by['trigger']) == len(fire_t), counts
    # on later firings the ring held >= pre_s (1 s at 5 Hz) of history just before them
    pos = {id(r): n for n, r in enumerate(raw)}
    for r in by['trigger'][1:]:
        n = pos[id(r)]
        pre = 0
        while n - 1 - pre >= 0 and raw[n - 1 - pre]['save'] == 'pre':
            pre += 1
        assert pre >= 4, (pre, counts)


def test_shard_rotation():
    with recorder(shard_max_frames=5) as (rec, root):
        name = rec.start(100, None)
        base = time.time()
        for k in range(14):
            rec.tick(base + k * 0.1, SMALL, {}, '', _never_views, t_mono=1 + k * 0.1)
        _finish(rec)
        d = _session(root, name)
        top = json.load(open(os.path.join(d, 'manifest.json')))
        assert top['complete'] is True and len(top['shards']) >= 2
        mans = sorted(glob.glob(os.path.join(d, 'shards', 's*', 'manifest.json')))
        assert len(mans) >= 2
        for m in mans:
            assert json.load(open(m))['closed'] is True
        assert len(recording_io.load_rows(d)) == 14


def test_queue_pressure_drops_not_blocks():
    real = SessionWriter._encode

    def slow(self, frame, row):
        time.sleep(0.15)
        return real(self, frame, row)

    with mock.patch.object(SessionWriter, '_encode', slow):
        with recorder(queue_max=1) as (rec, root):
            rec.start(1000, None)
            base = time.time()
            worst = 0.0
            for k in range(40):
                t = time.perf_counter()
                rec.tick(base + k * 0.01, SMALL, {}, '', _never_views, t_mono=1 + k * 0.01)
                worst = max(worst, time.perf_counter() - t)
            assert worst < 0.02, worst
            assert rec.status()['dropped'] > 0
            rec.stop()


def test_tick_p95_real_frames():
    frame = synthetic_frame(3)
    with recorder(queue_max=24) as (rec, root):
        rec.start(1000, None)
        base = time.time()
        times = []
        for k in range(300):
            t = time.perf_counter()
            rec.tick(base + k * 0.01, frame, {'seq': k}, '', _never_views, t_mono=1 + k * 0.01)
            times.append(time.perf_counter() - t)
        times.sort()
        p95 = times[int(len(times) * 0.95)]
        assert p95 < 0.003, p95
        rec.stop()


def test_stop_is_fast():
    with recorder() as (rec, root):
        rec.start(5, None)
        base = time.time()
        for k in range(10):
            rec.tick(base + k * 0.2, SMALL, {}, '', _never_views, t_mono=1 + k * 0.2)
        t = time.perf_counter()
        rec.stop('x')
        assert time.perf_counter() - t < 0.02
        assert not rec.active and rec.status()['stop_reason'] == 'x'
        rec.stop('again')   # idempotent
        rec.close(10)
        rec.close(10)


def test_crash_leaves_loadable_rows():
    with recorder() as (rec, root):
        name = rec.start(100, None)
        base = time.time()
        for k in range(8):
            rec.tick(base + k * 0.1, SMALL, {}, '', _never_views, t_mono=1 + k * 0.1)
        assert _wait(lambda: rec.status()['frames'] >= 8)
        rec._crash_for_test()
        rec.close(10)
        d = _session(root, name)
        assert len(recording_io.load_rows(d)) >= 8
        assert not os.path.exists(os.path.join(d, 'summary.json'))


def test_telemetry_rows():
    with recorder(telem_hz=5) as (rec, root):
        name = rec.start(1, None)
        base = time.time()
        for k in range(80):
            rec.tick(base + k * 0.05, SMALL, {}, '', _never_views, t_mono=10 + k * 0.05,
                     telem={'seq': k, 'found': True})
        n = rec.status()['telem_rows']
        _finish(rec)
        lines = []
        for p in glob.glob(os.path.join(_session(root, name), 'shards', 's*', 'telemetry.jsonl')):
            lines += [json.loads(x) for x in open(p) if x.strip()]
        assert 18 <= len(lines) <= 22, len(lines)
        assert len(lines) == n and 't' in lines[0] and 't_mono' in lines[0]


def test_views_fn_never_called_and_marks():
    with recorder() as (rec, root):
        name = rec.start(1, None)
        base = time.time()
        rec.tick(base, SMALL, {}, 'line_lost', _never_views, t_mono=1.0)
        rec.mark('bad')
        rec.tick(base + 0.1, SMALL, {}, '', _never_views, t_mono=1.1)
        st = rec.status()
        assert st['problems'] == 1 and st['marks'] == 1
        _finish(rec)
        rows = recording_io.load_rows(_session(root, name))
        assert [r.get('mark') for r in rows if r.get('mark')] == ['bad']
        assert any(r['save'] == 'mark' for r in rows)


def test_status_keys_and_writer_ends_itself():
    keys = {'on', 'session', 'frames', 'problems', 'marks', 'dropped', 'mb', 'elapsed_s',
            'free_gb', 'rate_hz', 'stop_reason', 'format', 'mode', 'shard', 'ring_s',
            'ring_mb', 'saved_by', 'telem_rows'}
    with recorder(min_free_gb=1e9) as (rec, root):
        assert keys <= set(rec.status()) and rec.status()['on'] is False
        rec.start(5, None)
        assert keys <= set(rec.status()) and rec.status()['format'] == 'v1'
        rec._writer.disk_check_s = 0.0
        assert _wait(lambda: not rec.active, 10)
        st = rec.status()
        assert st['on'] is False and st['stop_reason'] == 'disk full'
        # ticks after the writer ended are harmless
        rec.tick(time.time(), SMALL, {}, '', _never_views, t_mono=1.0)


def test_unique_session_names():
    with recorder() as (rec, root):
        a = rec.start(5, None)
        rec.stop()
        b = rec.start(5, None)
        rec.stop()
        c = rec.start(5, None, mode='bogus')
        assert a and b and c and len({a, b, c}) == 3
        assert rec.status()['mode'] == 'continuous'
        rec.stop()
        rec.close(10)
        for n in (a, b, c):
            assert os.path.exists(os.path.join(_session(root, n), 'summary.json'))


def test_many_starts_in_one_second_are_unique():
    # regression: the writer creates its folder asynchronously, so a name used a moment
    # ago (not only the previous one) must never be handed out again
    with recorder() as (rec, root):
        names = []
        for _ in range(6):
            names.append(rec.start(5, None))
            rec.stop()
        assert all(names) and len(set(names)) == 6, names
        rec.close(15)


def test_start_failure_and_never_started():
    with recorder() as (rec, root):
        rec.close(1)
        rec.event('x')
        rec.mark('y')
        rec.stop()
        rec.tick(time.time(), SMALL, {}, '', _never_views)
        assert rec.status()['on'] is False
    r2 = RecorderV1(os.path.join(_env.TMP, 'nonexistent-xyz'), min_free_gb=0.0)
    with mock.patch('recorder_v1.build_meta', side_effect=RuntimeError('boom')):
        assert r2.start(5, None) == '' and not r2.active


if __name__ == '__main__':
    _env.run_tests(globals())
