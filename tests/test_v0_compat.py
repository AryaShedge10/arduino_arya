"""Baseline: the current (v0) recorder, replay tool and web routes."""
import _env  # noqa: F401  (must be first)
import json
import os
import sys
import time

from fixtures import make_v0_session, synthetic_frame


def _fake_row(k):
    return {'seq': k, 'mode': 'AUTO', 'enabled': False,
            'line': {'found': True, 'offset_m': 0.0, 'angle_deg': 0.0},
            'obstacle': {'state': 'CLEAR', 'raw': 'CLEAR', 'distance_m': None}}


def test_recorder_writes_v0_session():
    import recorder
    rec = recorder.Recorder(root=_env.REC_DIR)
    name = rec.start(5.0, False)
    assert name, 'recorder did not start'
    base = time.time()
    for k in range(12):
        if k == 6:
            rec.mark('wrong')
        rec.tick(base + k * 0.25, synthetic_frame(k, 320, 180), _fake_row(k),
                 '', lambda: synthetic_frame(0, 320, 180))
    rec.stop('test')
    d = os.path.join(_env.REC_DIR, name)
    for f in ('meta.json', 'log.jsonl', 'events.jsonl', 'summary.json'):
        assert os.path.isfile(os.path.join(d, f)), f
    rows = [json.loads(l) for l in open(os.path.join(d, 'log.jsonl')) if l.strip()]
    assert len(rows) == 12, len(rows)
    assert all(os.path.isfile(os.path.join(d, r['file'])) for r in rows)
    assert any('mark' in r for r in rows)
    assert json.load(open(os.path.join(d, 'summary.json')))['frames'] == 12


def test_replay_main_on_fixture():
    import replay_recording
    d = make_v0_session(_env.REC_DIR, '20261018_120000', 10)
    old = sys.argv
    sys.argv = ['replay_recording.py', d]
    try:
        assert replay_recording.main() == 0
    finally:
        sys.argv = old


def test_web_routes():
    make_v0_session(_env.REC_DIR, '20261018_120001', 6)
    import web
    c = web.flask_app.test_client()
    r = c.get('/api/recordings')
    assert r.status_code == 200
    names = [s['name'] for s in r.get_json()['sessions']]
    assert '20261018_120001' in names, names
    assert c.get('/recordings/20261018_120001/log.jsonl').status_code == 200
    assert c.get('/review/20261018_120001').status_code == 200
    assert c.get('/recordings/20261018_120001/../x').status_code in (404, 308)


if __name__ == '__main__':
    _env.run_tests(globals())
