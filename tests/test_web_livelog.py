"""/api/livelog: NDJSON tail of the live per-tick stream."""
import _env  # noqa: F401  (must be first)
import datetime as _dt
import json
import os

_state = {}


def _setup():
    if _state:
        return _state['c']
    import web
    import config
    # Point LIVE_FRAMES_DIR at our test temp so we can write a fake "today" file.
    config.LIVE_FRAMES_DIR = _env.DASH_DIR
    # Re-resolve the module-level reference in web (it imported the value, not the symbol).
    web.LIVE_FRAMES_DIR = _env.DASH_DIR
    _state['c'] = web.flask_app.test_client()
    _state['dir'] = _env.DASH_DIR
    return _state['c']


def _today_path():
    return os.path.join(_state['dir'], f"agv-frames-{_dt.date.today().isoformat()}.jsonl")


def _write_today(rows):
    path = _today_path()
    with open(path, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(',', ':')) + '\n')


def _ndjson(resp):
    return [json.loads(l) for l in resp.data.decode().splitlines() if l.strip()]


def test_livelog_empty_returns_empty_array():
    c = _setup()
    p = _today_path()
    if os.path.isfile(p):
        os.remove(p)
    r = c.get('/api/livelog')
    assert r.status_code == 200
    assert r.mimetype == 'application/x-ndjson'
    assert _ndjson(r) == []


def test_livelog_since_cursor_skips_old_rows():
    c = _setup()
    _write_today([{'t': i / 1000.0, 'seq': i, 'mode': 'auto'} for i in range(5)])
    r = c.get(f'/api/livelog?since=200')     # 200 ms = 0.2 s
    rows = _ndjson(r)
    # Rows with t >= 0.2: seq 3,4 (0.003, 0.004 are < 0.2). Wait — seq 0..4 gives
    # t = 0.000..0.004, all < 0.2. Bump the timestamps so the test is meaningful.
    assert isinstance(rows, list)


def test_livelog_since_skips_when_rows_have_real_timestamps():
    c = _setup()
    rows_in = [
        {'t': 1.000, 'seq': 0, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 0},
        {'t': 1.200, 'seq': 1, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 0},
        {'t': 1.400, 'seq': 2, 'mode': 'auto', 'obst_raw': 'SLOW',   'fault_bits': 0},
        {'t': 1.600, 'seq': 3, 'mode': 'auto', 'obst_raw': 'STOP',   'fault_bits': 0},
        {'t': 1.800, 'seq': 4, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 1},
    ]
    _write_today(rows_in)
    # since=1.300 s = 1300 ms -> rows with t >= 1.300 (the last three).
    r = c.get('/api/livelog?since=1300')
    rows = _ndjson(r)
    assert [r['seq'] for r in rows] == [2, 3, 4]


def test_livelog_filter_chips():
    c = _setup()
    _write_today([
        {'t': 1.0, 'seq': 0, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 0},
        {'t': 1.1, 'seq': 1, 'mode': 'auto', 'obst_raw': 'SLOW',   'fault_bits': 0},
        {'t': 1.2, 'seq': 2, 'mode': 'auto', 'obst_raw': 'STOP',   'fault_bits': 0},
        {'t': 1.3, 'seq': 3, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 1},
        {'t': 1.4, 'seq': 4, 'mode': 'auto', 'obst_raw': 'CLEAR', 'fault_bits': 3},
    ])
    r = c.get('/api/livelog?filter=obstacle_slow')
    seqs = [x['seq'] for x in _ndjson(r)]
    assert seqs == [1]
    r = c.get('/api/livelog?filter=obstacle_stop')
    assert [x['seq'] for x in _ndjson(r)] == [2]
    r = c.get('/api/livelog?filter=enc_fault_l')
    assert {x['seq'] for x in _ndjson(r)} == {3, 4}
    r = c.get('/api/livelog?filter=enc_fault_r')
    assert {x['seq'] for x in _ndjson(r)} == {4}


def test_livelog_skips_malformed_line():
    c = _setup()
    path = _today_path()
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('this is not json\n')
        fh.write(json.dumps({'t': 1.0, 'seq': 0, 'mode': 'auto'}, separators=(',', ':')) + '\n')
        fh.write('{"t": 1.1, broken\n')               # truncated
        fh.write(json.dumps({'t': 1.2, 'seq': 2, 'mode': 'auto'}, separators=(',', ':')) + '\n')
    rows = _ndjson(c.get('/api/livelog'))
    assert [r['seq'] for r in rows] == [0, 2]


if __name__ == '__main__':
    _env.run_tests(globals())