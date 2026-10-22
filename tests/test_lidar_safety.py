"""Tests for the lidar package — parser, detectors, snapshot, thread safety.

These tests are pure-Python and never touch the real serial port. They
build synthetic 5-byte RPLIDAR frames byte-for-byte and feed them
through the parser, then exercise the detector logic via direct
manipulation of the internal rolling buffer (white-box).

Real-hardware end-to-end verification happens in the bench test at the
end of the LiDAR plan: with the rover on a stand, walk into the front
sector, verify the orchestrator e-stops.
"""
import _env  # noqa: F401  (must be first)
import collections
import math
import struct
import threading
import time

import lidar


# ─── helpers ─────────────────────────────────────────────────────────

def encode_node(angle_deg: float, dist_m: float, sync: bool = False,
                quality: int = 0) -> bytes:
    """Pack one 5-byte RPLIDAR measurement node.

    b0 = (sync << 0) | (s << 1) | ((quality & 0x3F) << 2)
    b1,b2 = angle as 15 bits of 1/64 degree: b2 carries bits 7..14 and
            b1:1..7 carries bits 0..6. b1 bit 0 is a status flag the
            parser ignores. Exact inverse of
            ``((b2 << 7) | (b1 >> 1)) & 0x7FFF``.
    b3,b4 = distance as u16 LE in q2 (0.25 mm) units.
    """
    ang_q6 = int(round(angle_deg * lidar.ANGLE_SCALE)) & 0x7FFF
    b1 = (ang_q6 << 1) & 0xFF
    b2 = (ang_q6 >> 7) & 0xFF
    dist_q2 = int(round(dist_m / lidar.DIST_Q2_TO_M)) & 0xFFFF
    b0 = ((quality & 0x3F) << 2) | (int(sync) << 1) | int(bool(sync))
    return bytes([b0, b1, b2]) + struct.pack('<H', dist_q2)


def encode_frame(*nodes) -> bytes:
    """Concatenate 5-byte nodes. (No header — the parser takes skip=0 for
    tests where the caller already removed the 7-byte header.)"""
    out = bytearray()
    for n in nodes:
        out.extend(n)
    return bytes(out)


def encode_full_response(*nodes) -> bytes:
    """Build a full SCAN response: 7-byte header + nodes."""
    return lidar.SCAN_HEADER + b"\x05\x00\x00\x00\x00" + encode_frame(*nodes)


# ─── parse_nodes: byte-level correctness ───────────────────────────

def test_parse_nodes_basic():
    """Encode a single node, parse it back, verify the recovered values."""
    buf = encode_frame(encode_node(45.0, 1.234))
    pts = list(lidar.parse_nodes(buf, skip=0, now=1000.0))
    assert len(pts) == 1, pts
    ts, ang, dist_m, q2 = pts[0]
    assert ts == 1000.0
    assert abs(ang - 45.0) < 0.02, f'angle {ang} too far from 45.0'
    assert abs(dist_m - 1.234) < 0.001, f'distance {dist_m} != 1.234'
    # q2 round-trip: dist_m / 0.00025
    assert q2 == int(round(1.234 / 0.00025))


def test_parse_nodes_many_angles():
    """A full revolution: 0/45/90/135/180/225/270/315 all round-trip."""
    buf = encode_frame(*[encode_node(a, 0.50) for a in
                         (0, 45, 90, 135, 180, 225, 270, 315)])
    pts = list(lidar.parse_nodes(buf, skip=0))
    assert len(pts) == 8
    recovered = [p[1] for p in pts]
    for expected, got in zip((0, 45, 90, 135, 180, 225, 270, 315), recovered):
        assert abs(expected - got) < 0.02, (expected, got)


def test_parse_nodes_drops_zero_distance():
    """A node with dist_q2 == 0 must be dropped (invalid return)."""
    zero_node = encode_node(0.0, 0.0)            # q2 = 0
    good_node = encode_node(0.0, 1.0)
    pts = list(lidar.parse_nodes(encode_frame(zero_node, good_node),
                                        skip=0))
    assert len(pts) == 1
    assert abs(pts[0][2] - 1.0) < 0.001


def test_parse_nodes_reaches_full_circle():
    """Regression: the angle decode must resolve all the way to 359.9°.

    An earlier formula masked b2 with 0xFE before shifting, which dropped
    bit 7 of the angle and put a hard ceiling at 357.95° — the final two
    degrees of every revolution were unreachable. Verified against the
    real sensor: 2036 consecutive samples decode with zero backward
    steps, max 359.95°, at full 1/64° resolution.
    """
    buf = encode_frame(*[encode_node(a, 2.0) for a in
                         (0, 90, 180, 270, 359.9, 359.98)])
    recovered = [p[1] for p in lidar.parse_nodes(buf, skip=0)]
    assert max(recovered) > 359.0, f'angle decode capped at {max(recovered)}'
    for expected, got in zip((0, 90, 180, 270, 359.9, 359.98), recovered):
        assert abs(expected - got) < 0.02, (expected, got)


def test_parse_nodes_respects_skip():
    """skip=N means the first N bytes are junk and the parser starts after."""
    junk = b"\xde\xad\xbe\xef"           # 4 junk bytes
    nodes = encode_frame(encode_node(10.0, 0.5), encode_node(20.0, 1.0))
    pts = list(lidar.parse_nodes(junk + nodes, skip=len(junk)))
    assert len(pts) == 2
    assert abs(pts[0][1] - 10.0) < 0.02
    assert abs(pts[1][1] - 20.0) < 0.02


def test_parse_nodes_truncated_tail():
    """Fewer than 5 bytes at the end are silently dropped (not an error)."""
    buf = encode_frame(encode_node(0.0, 0.5)) + b"\xab\xcd"   # 2 leftover
    pts = list(lidar.parse_nodes(buf, skip=0))
    assert len(pts) == 1


def test_parse_nodes_q2_units_match_sdk():
    """Regression: 4.0 m must decode to q2=16000, not 4000 (the mm mistake
    that ate an hour on the clone — see DIAGNOSIS.md)."""
    node = encode_node(0.0, 4.0)
    _b0, b1, b2, b3, b4 = node
    q2 = b3 | (b4 << 8)
    assert q2 == 16000, f'expected q2=16000 (4 m at 0.25 mm units), got {q2}'


# ─── find_scan_header ───────────────────────────────────────────────

def test_find_scan_header_present():
    buf = b"\xff\xff" + lidar.SCAN_HEADER + b"\x05\x00\x00\x00\x00" + b"\x00" * 20
    assert lidar.find_scan_header(buf) == 2


def test_find_scan_header_missing():
    assert lidar.find_scan_header(b"\x00" * 100) == -1


def test_find_scan_header_offset():
    """Multiple A5 bytes; the *paired* A5 5A is what we want."""
    buf = b"\xa5" + b"\x00" * 5 + lidar.SCAN_HEADER + b"\x05\x00\x00\x00\x00"
    assert lidar.find_scan_header(buf) == 6


# ─── LidarSnapshot: per-sector math + health verdicts ───────────────

def _feed(snd: lidar.LidarSafety, points, now: float = None) -> None:
    """White-box: stuff synthetic points into the rolling buffer as if the
    reader thread had just appended them. Timestamps are monotonic, which
    is the clock the reader and snapshot() both use."""
    if now is None:
        now = time.monotonic()
    with snd._lock:
        for ang, dist_m in points:
            q2 = int(round(dist_m / lidar.DIST_Q2_TO_M))
            snd._state.pts.append((now, ang, dist_m, q2))


def test_snapshot_front_sector_is_zero():
    """A 30° sector, 0° is forward (idx 0)."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000,
                                     window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(a, 2.0) for a in (0, 5, 10, 15, 20, 25, 28)])
    snap = snd.snapshot()
    assert snap.min_front_m == 2.0, snap
    assert snap.ok is True
    assert snap.sector_deg == 30
    assert snap.n_sectors == 12


def test_snapshot_sector_assignment():
    """30° sector_deg: 0-29 → 0, 30-59 → 1, …, 330-359 → 11."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    # Place a unique distance at 1° in each of 12 sectors.
    pts = [(i * 30 + 1, 1.0 + i * 0.1) for i in range(12)]
    _feed(snd, pts)
    snap = snd.snapshot()
    for i in range(12):
        expected = round(1.0 + i * 0.1, 3)
        got = snap.min_per_sector[i]
        assert abs(got - expected) < 0.01, (i, got, expected)


def test_snapshot_takes_min_per_sector():
    """Multiple points in the same sector → min wins."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(5, 3.0), (10, 0.5), (15, 2.0), (20, 1.0)])
    snap = snd.snapshot()
    assert snap.min_per_sector[0] == 0.5


def test_snapshot_filters_out_of_range():
    """Points outside LIDAR_FILTER_MIN_M..LIDAR_FILTER_MAX_M don't count."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    pts = [
        (0, 0.05),         # below min (housing reflection)
        (0, 13.0),         # above max (sky / wall behind)
        (0, 0.0),          # already dropped by parser (q2 == 0)
        (0, 0.6),          # valid
    ]
    _feed(snd, pts)
    snap = snd.snapshot()
    assert snap.min_per_sector[0] == 0.6
    assert snap.points_filtered == 1


def test_snapshot_stale():
    """If the most recent point is older than stale_ms, reason='stale'."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=200, window_sec=2.0)
    snd._state.connected = True
    # Feed points with a timestamp 1 s in the past.
    _feed(snd, [(0, 1.0), (90, 2.0)], now=time.monotonic() - 1.0)
    snap = snd.snapshot()
    assert snap.ok is False
    assert snap.reason == 'stale'
    assert snap.age_ms > 200


def test_snapshot_usb_disconnected():
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    # _state.connected is False by default
    snap = snd.snapshot()
    assert snap.ok is False
    # The exact reason text is the last_error, but it should be non-empty
    assert snap.reason != ''


def test_snapshot_stuck_detector():
    """A flat sweep — every sector reporting the same distance — held for
    > STUCK_HOLD_S means the sensor is replaying a buffer, not measuring
    a round room."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=10.0)
    snd._state.connected = True
    # A full turn of identical readings, like a stuck head against a wall.
    _feed(snd, [(i, 2.0) for i in range(120)], now=time.monotonic() - 3.0)
    with snd._lock:
        snd._state._stuck_anchor_ts = time.monotonic() - 3.0
    snap = snd.snapshot()
    assert snap.ok is False
    assert snap.reason == 'stuck'


def test_snapshot_static_room_is_not_stuck():
    """A wall straight ahead does not move: the front sector can read the
    same distance for minutes. That is a room, not a broken sensor, and
    must never stop the rover. Verified on the real sensor, where a
    stationary front sector read 4.05 / 4.06 / 4.05 / 4.07 m."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=10.0)
    snd._state.connected = True
    # Forward sector pinned at 4.05 m, the rest of the room varies as it
    # would with a real sweep.
    others = [(a, 2.0 + (a % 90) / 100.0) for a in range(30, 360, 2)]
    _feed(snd, [(a, 4.05) for a in range(0, 30, 2)] + others)
    with snd._lock:
        snd._state._stuck_anchor_ts = time.monotonic() - 30.0   # held a long time
    snap = snd.snapshot()
    assert snap.ok is True
    assert snap.reason == ''
    assert snap.min_front_m == 4.05


def test_snapshot_stuck_resets_when_room_varies():
    """Once the sweep varies again the hold is released."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=10.0)
    snd._state.connected = True
    _feed(snd, [(i, 2.0) for i in range(120)])
    with snd._lock:
        snd._state._stuck_anchor_ts = time.monotonic() - 3.0
    assert snd.snapshot().reason == 'stuck'
    # Room varies now: drop the flat points so only the new sweep remains.
    with snd._lock:
        snd._state.pts.clear()
    _feed(snd, [(a, 1.0 + a / 50.0) for a in range(0, 360, 3)])
    snap = snd.snapshot()
    assert snap.ok is True
    assert snap.reason == ''


def test_snapshot_motor_stopped():
    """If the angle span over the window is < 60° and we have data → motor
    is not spinning."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=5.0)
    snd._state.connected = True
    # 100 points all at angle 0 (a stationary head)
    _feed(snd, [(0, 1.0)] * 100)
    snap = snd.snapshot()
    assert snap.ok is False
    assert snap.reason == 'motor_stopped'


def test_snapshot_motor_running_is_ok():
    """A full revolution's worth of angles should not trip the motor detector."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=5.0)
    snd._state.connected = True
    # 360 evenly spaced points
    pts = [(a, 2.0) for a in (i * 1.0 for i in range(360))]
    _feed(snd, pts)
    snap = snd.snapshot()
    assert snap.reason != 'motor_stopped'
    assert snap.ok is True


def test_snapshot_noisy_detector():
    """More than NOISY_FRACTION of points are out of range → noisy."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    # 40 % noise, 60 % good
    good = [(0, 1.0)] * 6
    bad = [(0, 0.01)] * 4    # below filter min
    _feed(snd, good + bad)
    snap = snd.snapshot()
    assert snap.ok is False
    assert snap.reason == 'noisy'


def test_snapshot_min_front_inf_when_no_front_points():
    """If the front sector has zero points, min_front_m = inf."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(90, 1.0), (180, 2.0)])     # no front
    snap = snd.snapshot()
    assert snap.min_front_m == float('inf')


# ─── as_dict: JSON safety ───────────────────────────────────────────

def test_as_dict_no_inf_in_json():
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(90, 1.0)])        # no front points → inf in front
    snap = snd.snapshot()
    d = snap.as_dict()
    # Must be JSON-serialisable; inf becomes None.
    import json
    json.dumps(d)            # would raise if any non-JSON value
    assert d['min_front_m'] is None
    assert d['min_per_sector']['0'] is None


# ─── link_alive ─────────────────────────────────────────────────────

def test_link_alive_when_fresh():
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=500, window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(0, 1.0)])
    assert snd.link_alive() is True


def test_link_alive_when_disconnected():
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    # _state.connected is False
    _feed(snd, [(0, 1.0)])        # data is here but link not connected
    assert snd.link_alive() is False


def test_link_alive_when_stale():
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=200, window_sec=2.0)
    snd._state.connected = True
    _feed(snd, [(0, 1.0)], now=time.monotonic() - 1.0)
    assert snd.link_alive() is False


# ─── thread safety ──────────────────────────────────────────────────

def test_snapshot_is_thread_safe():
    """snapshot() must not crash when many threads call it at once while
    the (mock) reader thread is appending to the buffer."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=10000, window_sec=2.0)
    snd._state.connected = True
    stop = [False]

    def reader():
        i = 0
        while not stop[0]:
            ang = (i * 3.6) % 360
            _feed(snd, [(ang, 1.0 + (i % 5) * 0.1)])
            i += 1
            time.sleep(0.0005)

    def reader2():
        i = 0
        while not stop[0]:
            ang = (i * 7.3) % 360
            _feed(snd, [(ang, 2.0)])
            i += 1
            time.sleep(0.0005)

    threads = [threading.Thread(target=reader, daemon=True),
               threading.Thread(target=reader2, daemon=True)]
    errors = []

    def poller():
        try:
            for _ in range(500):
                snd.snapshot()
        except Exception as exc:        # pragma: no cover
            errors.append(exc)

    pollers = [threading.Thread(target=poller, daemon=True) for _ in range(4)]
    for t in threads + pollers:
        t.start()
    for t in pollers:
        t.join(timeout=5.0)
    stop[0] = True
    for t in threads:
        t.join(timeout=2.0)
    assert not errors, errors


# ─── disabled-in-config behaviour ───────────────────────────────────

def test_disabled_returns_no_health():
    """If LIDAR_ENABLED=0, connect() returns False and snapshot() reports
    not-ok without ever trying to open the port."""
    snd = lidar.LidarSafety(enabled=False, port='/dev/should/never/open')
    assert snd.connect() is False
    snap = snd.snapshot()
    assert snap.ok is False
    assert 'disabled' in snap.reason or 'usb' in snap.reason


# ─── close() idempotence ────────────────────────────────────────────

def test_close_when_never_connected():
    """close() must not crash if connect() was never called."""
    snd = lidar.LidarSafety(enabled=False)
    snd.close()      # no exception
    snd.close()      # idempotent


# ─── sanity: round-trip of an entire SCAN response ─────────────────

def test_full_scan_response_round_trip():
    """Build a realistic 7-byte header + many nodes, ensure parse_nodes
    yields the same number of points we put in."""
    nodes = [encode_node(a, 1.0) for a in (i * 5.0 for i in range(72))]
    buf = encode_full_response(*nodes)
    assert buf[:2] == lidar.SCAN_HEADER
    parsed = list(lidar.parse_nodes(buf, skip=lidar.SCAN_HEADER_LEN))
    assert len(parsed) == 72
    # The first point's angle should round-trip close to 0 (modulo wrap).
    assert abs(parsed[0][1] - 0.0) < 0.02


# ── reader thread robustness ───────────────────────────────────────

class _FakeSerial:
    """Raises the same exception pyserial raises when close() pulls the
    port out from under an in-flight read (it sets ``fd`` to None)."""

    def __init__(self, exc):
        self._exc = exc

    def read(self, _n):
        raise self._exc

    def close(self):
        pass


class _ChunkSerial:
    """Feeds a fixed list of byte chunks, then returns b'' forever.

    Exercises the reader's real work — header sync, the 5-byte stride,
    the parse loop — which the raising fake never reached.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _n):
        time.sleep(0.01)
        return self._chunks.pop(0) if self._chunks else b''

    def close(self):
        pass


def _start_reader(snd):
    snd._state.connected = True
    snd._state.running = True
    snd._reader = threading.Thread(target=snd._reader_loop,
                                   name='lidar-reader', daemon=True)
    snd._reader.start()
    return snd


def _reader_lidar(exc):
    """A LidarSafety whose reader thread hits ``exc`` on every read."""
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=1000, window_sec=2.0)
    snd._serial = _FakeSerial(exc)
    return _start_reader(snd)


def _frame_lidar(chunks, **kw):
    snd = lidar.LidarSafety(sector_deg=30, stale_ms=1000, window_sec=5.0, **kw)
    snd._serial = _ChunkSerial(chunks)
    return _start_reader(snd)


def _wait_for_points(snd, n, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with snd._lock:
            if len(snd._state.pts) >= n:
                return True
        time.sleep(0.01)
    return False


# ─── reader thread: the parse path, not just the error path ─────────

def test_reader_parses_a_real_scan_response():
    """Regression: the reader references names it never imported, so the
    thread died on its first real frame. Nothing in the unit suite
    reached this code — the only other fake serial raised on every read.
    """
    frame = encode_full_response(*[encode_node(a, 1.5) for a in
                                   (i * 5.0 for i in range(72))])
    snd = _frame_lidar([frame])
    try:
        assert _wait_for_points(snd, 72), \
            f'only {len(snd._state.pts)} points parsed from 72 nodes'
        assert snd._reader.is_alive(), 'reader thread died mid-frame'
    finally:
        snd.close()


def test_reader_syncs_when_the_header_is_split_across_reads():
    """USB delivers arbitrary chunk sizes, so A5 and 5A can land in
    separate reads. The reader must not consume anything until it has
    seen all SCAN_HEADER_LEN header bytes."""
    frame = encode_full_response(*[encode_node(a, 2.0) for a in
                                   (i * 10.0 for i in range(36))])
    snd = _frame_lidar([frame[:1], frame[1:3], frame[3:9], frame[9:]])
    try:
        assert _wait_for_points(snd, 36), \
            f'only {len(snd._state.pts)} points parsed from a split header'
    finally:
        snd.close()


def test_reader_does_not_resync_after_the_first_frame():
    """A5 5A can appear inside node data. Re-syncing mid-stream would
    destroy the 5-byte stride, so the reader must ignore it after the
    first frame and keep parsing on the stride."""
    frame = encode_full_response(*[encode_node(a, 2.0) for a in
                                   (i * 10.0 for i in range(36))])
    # Junk that looks like a second header, appended right where the
    # stride expects a node.
    with_fake_header = frame + b"\xA5\x5A" + b"\x00" * 13
    snd = _frame_lidar([with_fake_header])
    try:
        time.sleep(0.2)
        angles = sorted(p[1] for p in snd._state.pts)
        assert len(angles) == 36, f'{len(angles)} points, expected 36'
        assert abs(angles[0] - 0.0) < 0.02
    finally:
        snd.close()


def test_reader_survives_junk_before_the_header():
    """Bytes ahead of the header are noise from a mid-stream reset; the
    reader should skip to the header, not parse from offset 0.

    (Noise that itself contains A5 5A is indistinguishable from the real
    header at the byte level — there is nothing to tell them apart — so
    the guarantee here is only about noise that doesn't look like one.)
    """
    junk = b"\x11\x22\x33\x44\x55"
    frame = encode_full_response(*[encode_node(a, 1.0) for a in
                                   (i * 15.0 for i in range(24))])
    snd = _frame_lidar([junk + frame])
    try:
        assert _wait_for_points(snd, 24), \
            f'only {len(snd._state.pts)} points parsed past the junk'
    finally:
        snd.close()


def test_reader_keeps_waiting_when_the_stream_has_no_header():
    """Noise that never becomes a header must not wedge the reader — it
    keeps looking, and syncs as soon as a real frame shows up."""
    frame = encode_full_response(*[encode_node(a, 1.0) for a in
                                   (i * 30.0 for i in range(12))])
    snd = _frame_lidar([b"\x99" * 64, b"\x01\x02\x03", frame])
    try:
        assert _wait_for_points(snd, 12), \
            f'only {len(snd._state.pts)} points parsed after the noise'
    finally:
        snd.close()


def test_reader_survives_close_during_read():
    """Regression: pyserial nulls ``fd`` on close(), so a read already in
    flight raises TypeError rather than SerialException. Catching only
    the serial errors let that escape and kill the reader thread, taking
    obstacle safety down with it."""
    snd = _reader_lidar(TypeError("'NoneType' object cannot be interpreted as an integer"))
    time.sleep(0.2)
    assert snd._reader.is_alive(), 'reader thread died on a TypeError'
    snd.close()


def test_reader_survives_serial_exception():
    snd = _reader_lidar(lidar.serial.SerialException('unplugged'))
    time.sleep(0.2)
    assert snd._reader.is_alive(), 'reader thread died on a SerialException'
    snd.close()


def test_reader_stops_cleanly_on_close():
    """close() must end the thread promptly, without logging a fault."""
    snd = _reader_lidar(lidar.serial.SerialException('unplugged'))
    reader = snd._reader          # close() drops the reference
    snd.close()
    reader.join(timeout=2.0)
    assert not reader.is_alive(), 'close() did not stop the reader'


if __name__ == '__main__':
    _env.run_tests(globals())
