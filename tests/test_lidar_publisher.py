"""Tests for lidar/publisher.py — the points.json writer behind the
dashboard's polar overlay.

Every test here runs against a fake LiDAR that only implements
``recent_points(window_s, limit)``, which is the whole contract the
publisher depends on. No sensor, no serial port, no threads beyond the
one the publisher owns.
"""
import _env  # noqa: F401  (must be first)
import json
import logging
import os
import tempfile
import threading
import time

import lidar
from lidar import PointsPublisher


class FakeLidar:
    """Stands in for LidarSafety's rolling buffer."""

    def __init__(self, points=None):
        self.points = list(points or [])
        self.calls = 0
        self.raise_on_call = False

    def recent_points(self, window_s=None, limit=None):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError('sensor on fire')
        pts = self.points
        if limit and len(pts) > limit:
            stride = len(pts) / limit
            pts = [pts[int(i * stride)] for i in range(limit)]
        return list(pts)


def _zones(stop=0.50, slow=1.00):
    return lambda: (stop, slow)


def _pts(n=5, base_ts=None):
    base = base_ts if base_ts is not None else time.monotonic()
    return [(base + i * 0.01, i * 10.0, 1.0 + i * 0.1,
             int((1.0 + i * 0.1) * 4000)) for i in range(n)]


def _pub(out_dir, fake=None, zones=None, **kw):
    return PointsPublisher(fake or FakeLidar(), out_dir,
                           zones_source=zones or _zones(), **kw)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ─── the payload the dashboard actually reads ────────────────────────

def test_payload_has_every_key_the_canvas_needs():
    """The canvas reads updated_at, window_s, n, points, and the two
    zone distances. A missing key shows up as a frozen ring, not an
    error, so assert on all of them explicitly."""
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d).publish_once()
    for key in ('updated_at', 'window_s', 'n', 'points',
                'lidar_stop_dist_m', 'lidar_slow_dist_m'):
        assert key in payload, f'missing {key} in points.json'
    assert payload['window_s'] == 2.0
    assert payload['n'] == 0
    assert payload['points'] == []


def test_zone_distances_come_from_zones_source():
    """Bug: the writer used to hard-code 0.5/1.0, so the canvas rings
    never moved when an operator retuned a zone."""
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, zones=_zones(0.30, 0.90)).publish_once()
    assert payload['lidar_stop_dist_m'] == 0.30
    assert payload['lidar_slow_dist_m'] == 0.90


def test_zone_distances_are_re_read_every_publish():
    """A zones_source is a live view, not a startup snapshot: change what
    it returns between two publishes and the payload must follow."""
    zones = {'stop': 0.50, 'slow': 1.00}
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, zones=lambda: (zones['stop'], zones['slow']))
        assert pub.publish_once()['lidar_stop_dist_m'] == 0.50
        zones['stop'], zones['slow'] = 0.20, 0.60
        second = pub.publish_once()
    assert second['lidar_stop_dist_m'] == 0.20
    assert second['lidar_slow_dist_m'] == 0.60


def test_each_point_is_tagged_with_a_zone():
    """Points inside stop / between stop and slow / beyond slow get
    stop / slow / clear, so the canvas can colour them."""
    now = time.monotonic()
    fake = FakeLidar([(now, 0.0, 0.30, 1200),      # < stop
                      (now, 10.0, 0.70, 2800),     # stop..slow
                      (now, 20.0, 3.00, 12000)])   # > slow
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, fake, zones=_zones(0.50, 1.00)).publish_once()
    assert [p['zone'] for p in payload['points']] == ['stop', 'slow', 'clear']


def test_zone_boundaries_are_half_open():
    """A point exactly at stop distance is 'slow', exactly at slow is
    'clear' — the zones are [0, stop), [stop, slow), [slow, inf)."""
    now = time.monotonic()
    fake = FakeLidar([(now, 0.0, 0.50, 2000), (now, 10.0, 1.00, 4000)])
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, fake, zones=_zones(0.50, 1.00)).publish_once()
    assert [p['zone'] for p in payload['points']] == ['slow', 'clear']


def test_t_is_relative_to_the_oldest_point():
    """t=0 anchors the window; ages stay positive and inside it."""
    fake = FakeLidar(_pts(5))
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, fake).publish_once()
    ts = [p['t'] for p in payload['points']]
    assert ts[0] == 0.0
    assert all(b > a for a, b in zip(ts, ts[1:]))
    assert ts[-1] < 2.0, ts


def test_output_dir_is_created():
    """start() must make the directory — the dashboard's dir is often
    missing on a fresh boot."""
    with tempfile.TemporaryDirectory() as d:
        nested = os.path.join(d, 'deep', 'dashboard')
        assert not os.path.isdir(nested)
        pub = _pub(nested)
        try:
            pub.start()
            assert os.path.isdir(nested)
        finally:
            pub.stop()


def test_publish_writes_valid_json_to_disk():
    """The real failure mode of the old writer: it raised NameError on
    the very first os.makedirs, so nothing was ever written."""
    with tempfile.TemporaryDirectory() as d:
        _pub(d, FakeLidar(_pts(3))).publish_once()
        path = os.path.join(d, 'points.json')
        assert os.path.exists(path)
        data = json.loads(open(path).read())
    assert data['n'] == 3
    assert len(data['points']) == 3


def test_write_is_atomic_leaving_no_tmp_behind():
    """Write-then-rename: the reader never sees a half-written file and
    the tmp file does not survive a successful publish."""
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, FakeLidar(_pts(3)))
        for _ in range(5):
            pub.publish_once()
            assert not os.path.exists(os.path.join(d, 'points.json.tmp'))


def test_limit_downsamples_a_large_buffer():
    """A spinning head makes far more points in 2 s than the canvas can
    draw; the writer strides down to ``limit``."""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeLidar(_pts(2000))
        payload = _pub(d, fake, limit=500).publish_once()
    assert payload['n'] == 500
    assert fake.calls == 1


def test_empty_buffer_is_a_valid_payload():
    """No points yet must still produce a parseable file, or the canvas
    shows a hard fetch error instead of an empty plot."""
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, FakeLidar([])).publish_once()
    assert payload['n'] == 0 and payload['points'] == []
    assert payload['lidar_stop_dist_m'] == 0.50   # still present


# ─── thread lifecycle ───────────────────────────────────────────────

def test_thread_writes_without_being_asked():
    """start() alone must produce a file — nothing calls publish_once()
    in production."""
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, FakeLidar(_pts(4)), interval_s=0.02)
        try:
            pub.start()
            path = os.path.join(d, 'points.json')
            assert _wait_for(lambda: os.path.exists(path)), \
                'publisher thread never wrote'
        finally:
            pub.stop()


def test_thread_is_a_daemon():
    """A publisher that outlived the process as a non-daemon would hang
    shutdown."""
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d)
        try:
            pub.start()
            writers = [t for t in threading.enumerate()
                       if t.name == 'lidar-points-writer']
            assert writers, 'no publisher thread running'
            assert all(t.daemon for t in writers)
        finally:
            pub.stop()


def test_stop_ends_the_thread_promptly():
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, FakeLidar(_pts(4)), interval_s=0.05)
        pub.start()
        thread = pub._thread
        pub.stop()
        assert not thread.is_alive(), 'stop() did not end the writer thread'
        assert 'lidar-points-writer' not in [t.name for t in threading.enumerate()]


def test_stop_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d)
        pub.start()
        pub.stop()
        pub.stop()      # no exception on a second call


def test_stop_before_start_is_safe():
    with tempfile.TemporaryDirectory() as d:
        _pub(d).stop()


def test_restart_makes_a_fresh_thread():
    """start → stop → start must not leave two writers racing on the
    same file (the old writer could not be stopped and restarted at
    all)."""
    with tempfile.TemporaryDirectory() as d:
        pub = _pub(d, FakeLidar(_pts(3)), interval_s=0.02)
        pub.start()
        pub.stop()
        pub.start()
        try:
            live = [t for t in threading.enumerate()
                    if t.name == 'lidar-points-writer']
            assert len(live) == 1, f'{len(live)} writers running'
        finally:
            pub.stop()


# ─── failure handling ───────────────────────────────────────────────

def test_a_failing_tick_is_logged_and_the_loop_continues():
    """The old loop swallowed every exception with a bare
    ``except Exception: pass``, so the file silently stopped updating
    with nothing in the log. A failure must be visible, and must not
    kill the thread."""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeLidar(_pts(3))
        fake.raise_on_call = True
        pub = _pub(d, fake, interval_s=0.02)
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.getLogger('agv.lidar').addHandler(handler)
        try:
            pub.start()
            time.sleep(0.3)
            assert any('points.json writer tick failed' in m for m in records), records
            assert pub._thread.is_alive(), 'the writer died on an exception'
        finally:
            pub.stop()
            logging.getLogger('agv.lidar').removeHandler(handler)


def test_recovery_after_a_failed_tick():
    """Once the fault clears, publishing resumes without a restart."""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeLidar(_pts(3))
        fake.raise_on_call = True
        pub = _pub(d, fake, interval_s=0.02)
        pub.start()
        time.sleep(0.15)
        fake.raise_on_call = False
        try:
            path = os.path.join(d, 'points.json')
            assert _wait_for(lambda: os.path.exists(path)), 'never recovered'
            assert json.loads(open(path).read())['n'] == 3
        finally:
            pub.stop()


def test_a_failed_tick_leaves_the_last_good_payload_intact():
    """The rename is what makes the file safe to read: a tick that dies
    before it leaves the previous complete payload in place rather than
    a truncated one."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'points.json')
        _pub(d, FakeLidar(_pts(3))).publish_once()
        good = json.loads(open(path).read())

        fake = FakeLidar(_pts(3))
        fake.raise_on_call = True
        pub = _pub(d, fake, interval_s=0.02)
        pub.start()
        try:
            time.sleep(0.15)                    # several failing ticks
            assert json.loads(open(path).read()) == good
        finally:
            pub.stop()


# ─── the real integration point ─────────────────────────────────────

def test_publisher_reads_a_real_lidar_safety():
    """End-to-end against the actual sensor object (disabled, so no port
    is opened) with points stuffed into its rolling buffer."""
    snd = lidar.LidarSafety(enabled=False, sector_deg=30, window_sec=2.0)
    now = time.monotonic()
    with snd._lock:
        for i in range(50):
            snd._state.pts.append((now - 0.5 + i * 0.001, i * 7.0, 2.0, 8000))
    with tempfile.TemporaryDirectory() as d:
        payload = _pub(d, snd, zones=_zones(0.50, 1.00)).publish_once()
    assert payload['n'] == 50
    assert payload['points'][0]['t'] == 0.0


def test_recent_points_window_is_honoured():
    """Points older than the window must not reach the canvas, or the
    trail keeps drawing a room the rover has already left."""
    snd = lidar.LidarSafety(enabled=False, window_sec=2.0)
    now = time.monotonic()
    with snd._lock:
        snd._state.pts.append((now - 5.0, 0.0, 1.0, 4000))     # too old
        snd._state.pts.append((now - 0.1, 10.0, 2.0, 8000))    # fresh
    pts = snd.recent_points(window_s=2.0)
    assert len(pts) == 1
    assert pts[0][1] == 10.0


if __name__ == '__main__':
    _env.run_tests(globals())