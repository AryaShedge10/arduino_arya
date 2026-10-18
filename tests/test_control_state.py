"""control_state record_mode + /api/record mode (isolated temp control.json)."""
import _env  # noqa: F401  (must be first)
import json
import os

import config
import control_state
import web

assert control_state.CONTROL_PATH.startswith(_env.TMP), control_state.CONTROL_PATH


def _tmp(name):
    d = os.path.join(_env.TMP, name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'control.json')


def test_old_file_without_key():
    p = _tmp('old')
    json.dump({'enabled': False, 'record': True, 'record_rate': 5}, open(p, 'w'))
    st = control_state.read_state(p)
    assert st['record_mode'] == config.REC_MODE_DEFAULT == 'continuous'
    assert control_state.read_state(_tmp('missing'))['record_mode'] == 'continuous'


def test_round_trip():
    p = _tmp('rt')
    assert control_state.write_state(record_mode='triggered', path=p)['record_mode'] == 'triggered'
    assert control_state.read_state(p)['record_mode'] == 'triggered'
    control_state.write_state(record=True, path=p)          # other writes keep it
    assert control_state.read_state(p)['record_mode'] == 'triggered'
    control_state.write_state(record_mode='continuous', path=p)
    assert control_state.read_state(p)['record_mode'] == 'continuous'


def test_invalid_gives_default():
    p = _tmp('bad')
    json.dump({'record_mode': 'bogus'}, open(p, 'w'))
    assert control_state.read_state(p)['record_mode'] == 'continuous'
    control_state.write_state(record_mode='triggered', path=p)
    assert control_state.write_state(record_mode='nope', path=p)['record_mode'] == 'continuous'
    assert control_state.read_state(p)['record_mode'] == 'continuous'


def test_api_record_mode():
    c = web.flask_app.test_client()
    old = ('record', 'rate', 'views', 'rec_on', 'rec_session', 'rec_frames', 'rec_problems', 'rec_marks',
           'rec_dropped', 'rec_mb', 'rec_elapsed_s', 'rec_free_gb', 'rec_stop_reason')
    d = c.get('/api/record').get_json()
    assert all(k in d for k in old), d
    assert all(k in d for k in ('mode', 'rec_format', 'rec_mode', 'rec_shard', 'rec_ring_s'))
    assert d['mode'] == 'continuous'
    r = c.post('/api/record', json={'action': 'start', 'rate': 5, 'mode': 'triggered'})
    assert r.status_code == 200 and r.get_json()['mode'] == 'triggered'
    assert c.get('/api/record').get_json()['mode'] == 'triggered'
    assert json.load(open(control_state.CONTROL_PATH))['record_mode'] == 'triggered'
    assert c.post('/api/record', json={'action': 'stop'}).status_code == 200
    # start without mode keeps the previous one
    r = c.post('/api/record', json={'action': 'start', 'rate': 5}).get_json()
    assert r['mode'] == 'triggered' and r['record'] is True
    r = c.post('/api/record', json={'action': 'start', 'rate': 5, 'mode': 'junk'}).get_json()
    assert r['mode'] == 'continuous'
    r = c.post('/api/record', json={'action': 'stop'})
    assert r.status_code == 200 and r.get_json()['record'] is False


if __name__ == '__main__':
    _env.run_tests(globals())
