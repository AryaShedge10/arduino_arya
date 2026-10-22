"""frame_log: rate-limited append-only writer with daily rotation."""
import _env  # noqa: F401  (must be first)
import json
import os
import tempfile
import threading
import time

import frame_log


def _tmp():
    return tempfile.mkdtemp(prefix='fl-', dir=_env.TMP)


def _read(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _row(t, **kw):
    return {'t': t, 't_mono': t, 'seq': kw.get('seq', 0), 'mode': 'auto',
            'enabled': True, 'v': 0.1, 'omega': 0.0}


def test_write_appends_one_line():
    d = _tmp()
    fl = frame_log.FrameLog(base_dir=d, hz=1.0)  # 1 Hz throttle
    fl.start()
    try:
        # Three writes back-to-back: only the first should hit the file.
        assert fl.write(_row(1.0)) is True
        assert fl.write(_row(1.0)) is False
        time.sleep(1.05)
        assert fl.write(_row(2.0)) is True
        rows = _read(fl.current_path)
        assert len(rows) == 2
        assert rows[0]['t'] == 1.0 and rows[1]['t'] == 2.0
    finally:
        fl.close()


def test_rotation_at_midnight():
    d = _tmp()
    fl = frame_log.FrameLog(base_dir=d, hz=10.0)
    fl.start()
    try:
        assert fl.write(_row(1.0)) is True
        path_today = fl.current_path
        assert os.path.isfile(path_today)
        # Advance the throttle clock so the next write isn't dropped, then
        # simulate the date rolling forward by patching frame_log.date (the
        # module binding — datetime.date itself cannot be patched).
        fl._last_mono = 0.0
        real_date = frame_log.date
        frame_log.date = type('D', (), {'today': staticmethod(lambda: __import__('datetime').date(2099, 1, 2))})()
        try:
            assert fl.write(_row(2.0)) is True
        finally:
            frame_log.date = real_date
        # A fresh file for the new date should exist; the old one stays.
        new_path = fl.current_path
        assert new_path != path_today
        assert os.path.isfile(new_path)
        old_rows = _read(path_today)
        new_rows = _read(new_path)
        assert len(old_rows) == 1 and old_rows[0]['t'] == 1.0
        assert len(new_rows) == 1 and new_rows[0]['t'] == 2.0
    finally:
        fl.close()


def test_idempotent_close():
    d = _tmp()
    fl = frame_log.FrameLog(base_dir=d, hz=10.0)
    fl.start()
    fl.write(_row(1.0))
    fl.close()
    fl.close()                # must not raise
    assert fl.current_path == ''
    # A second start() should re-open fresh.
    fl.start()
    assert fl.write(_row(2.0)) is True
    fl.close()


def test_concurrent_write_and_close():
    d = _tmp()
    fl = frame_log.FrameLog(base_dir=d, hz=200.0)  # very high so the throttle never kicks in
    fl.start()
    errors: list = []

    def writer():
        try:
            for i in range(50):
                fl.write(_row(time.time()))
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.01)
    fl.close()                # interleaved close
    t.join(timeout=2)
    assert not errors
    assert fl.current_path == ''


def test_schema_matches_extended_telem_row():
    """The on-disk JSON should at least include the fields the dashboard reads."""
    d = _tmp()
    fl = frame_log.FrameLog(base_dir=d, hz=10.0)
    fl.start()
    try:
        row = {'t': 1.0, 't_mono': 1.0, 'seq': 0, 'mode': 'auto', 'enabled': True,
               'v': 0.1, 'omega': -0.2, 'ticks_l': 1234, 'ticks_r': -1200,
               'offset_m': -0.012, 'angle_deg': 1.5, 'obst_raw': 'CLEAR',
               'applied_l': 50, 'applied_r': 49, 'fault_bits': 0}
        assert fl.write(row) is True
        rows = _read(fl.current_path)
        assert len(rows) == 1
        assert rows[0]['v'] == 0.1 and rows[0]['obst_raw'] == 'CLEAR'
        # fault_bits=0 means no errors key (omitted when empty).
        assert 'errors' not in rows[0]
    finally:
        fl.close()


if __name__ == '__main__':
    _env.run_tests(globals())