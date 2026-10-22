"""/api/sessions/latest + /api/sessions/<name>/timeline[.csv]"""
import _env  # noqa: F401  (must be first)
import csv as _csv
from io import StringIO as _StringIO

from fixtures import make_v1_session

S1, S2 = '20261018_130000', '20261019_140000'
_state = {}


def _setup():
    if _state:
        return _state['c']
    import web
    make_v1_session(_env.REC_DIR, S1, 12)
    make_v1_session(_env.REC_DIR, S2, 7)
    _state['c'] = web.flask_app.test_client()
    return _state['c']


def test_sessions_latest_returns_newest():
    c = _setup()
    r = c.get('/api/sessions/latest')
    assert r.status_code == 200
    info = r.get_json()
    assert info['name'] == S2                # newer by date string
    assert info['n'] >= 1 and info['schema'] == 1


def test_sessions_latest_404_when_empty():
    import os, shutil, tempfile
    import web
    tmp = tempfile.mkdtemp(prefix='sesslatest-', dir=_env.TMP)
    web.REC_DIR = tmp
    c = web.flask_app.test_client()
    r = c.get('/api/sessions/latest')
    assert r.status_code == 404
    assert r.get_json() == {'error': 'no_sessions'}
    shutil.rmtree(tmp, ignore_errors=True)
    web.REC_DIR = _env.REC_DIR                 # restore for other tests


def test_timeline_range():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline?from=2&to=5')
    assert r.status_code == 200
    j = r.get_json()
    assert j['total'] == 12 and j['from'] == 2 and j['to'] == 5
    assert len(j['rows']) == 4
    # Rows come from load_rows with sort=True, so i is non-decreasing.
    is_ = [row.get('i') for row in j['rows']]
    assert is_ == sorted(is_)


def test_timeline_default_range():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline')
    j = r.get_json()
    assert j['from'] == 0 and j['to'] == 11 and len(j['rows']) == 12


def test_timeline_fields_projection():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline?fields=t,i,seq')
    rows = r.get_json()['rows']
    assert rows and set(rows[0].keys()) == {'t', 'i', 'seq'}


def test_timeline_404_unknown_session():
    c = _setup()
    r = c.get('/api/sessions/19990101_000000/timeline')
    assert r.status_code == 404
    assert r.get_json() == {'error': 'unknown_session'}


def test_timeline_416_out_of_range():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline?from=999')
    assert r.status_code == 416
    assert r.get_json().get('error') == 'range_out_of_bounds'
    assert r.get_json().get('total') == 12


def test_timeline_csv_content_type_and_header():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline.csv?from=0&to=2')
    assert r.status_code == 200
    assert r.mimetype == 'text/csv'
    assert 'attachment' in r.headers.get('Content-Disposition', '')
    text = r.data.decode()
    reader = _csv.DictReader(_StringIO(text))
    rows = list(reader)
    assert len(rows) == 3
    # First header column is whatever the flattener saw first; assert shape only.
    assert rows[0]                     # non-empty


def test_timeline_bad_range_400():
    c = _setup()
    r = c.get(f'/api/sessions/{S1}/timeline?from=abc')
    assert r.status_code == 400


if __name__ == '__main__':
    _env.run_tests(globals())