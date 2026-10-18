"""T15: v1-aware read endpoints, traversal safety, tar exclusion."""
import _env  # noqa: F401  (must be first)
import io
import json
import os
import tarfile

from fixtures import make_v0_session, make_v1_session

V0, V1 = '20261018_120000', '20261018_130000'
_state = {}


def _setup():
    if _state:
        return _state['c']
    import web
    import recording_io
    assert os.path.realpath(str(web.REC_DIR)) == os.path.realpath(_env.REC_DIR), web.REC_DIR
    make_v0_session(_env.REC_DIR, V0, 10)
    make_v1_session(_env.REC_DIR, V1, 12)
    _state['c'] = web.flask_app.test_client()
    _state['io'] = recording_io
    return _state['c']


def _ndjson(resp):
    return [json.loads(l) for l in resp.data.decode().splitlines() if l.strip()]


def test_list_both_schemas():
    c = _setup()
    r = c.get('/api/recordings')
    assert r.status_code == 200
    by = {s['name']: s for s in r.get_json()['sessions']}
    for n, schema in ((V0, 0), (V1, 1)):
        s = by[n]
        assert s['frames'] > 0 and s['schema'] == schema and s['mode'], s
        for k in ('name', 'session', 'frames', 'size_mb', 'done', 'active'):
            assert k in s, (k, s)
    assert by[V0]['mode'] == 'continuous' and by[V1]['mode'] == 'continuous'


def test_merged_log_matches_loader():
    c = _setup()
    rio = _state['io']
    for n in (V0, V1):
        r = c.get(f'/api/recordings/{n}/log.jsonl')
        assert r.status_code == 200 and r.mimetype == 'application/x-ndjson'
        assert r.headers['Cache-Control'] == 'no-cache'
        got = _ndjson(r)
        want = rio.load_rows(os.path.join(_env.REC_DIR, n))
        assert len(got) == len(want) > 0
        assert [x.get('i') for x in got] == [x.get('i') for x in want]
        assert [x.get('i') for x in got] == sorted(x.get('i') for x in got)


def test_events_and_meta():
    c = _setup()
    for n in (V0, V1):
        r = c.get(f'/api/recordings/{n}/events.jsonl')
        assert r.status_code == 200 and r.mimetype == 'application/x-ndjson'
        assert len(_ndjson(r)) >= 1
        m = c.get(f'/api/recordings/{n}/meta')
        assert m.status_code == 200 and m.is_json
        j = m.get_json()
        assert set(j) == {'schema', 'meta', 'summary', 'manifest_totals'}, j
        assert j['meta']['session'] == n
    assert c.get(f'/api/recordings/{V1}/meta').get_json()['manifest_totals']['frames'] == 12
    assert c.get(f'/api/recordings/{V0}/meta').get_json()['manifest_totals'] is None
    for ep in ('log.jsonl', 'events.jsonl', 'meta'):
        assert c.get(f'/api/recordings/20200101_000000/{ep}').status_code == 404
        assert c.get(f'/api/recordings/x/{ep}').status_code == 404


def test_files_served():
    c = _setup()
    r = c.get(f'/recordings/{V1}/shards/s0000/frames/000000.jpg')
    assert r.status_code == 200 and r.mimetype == 'image/jpeg'
    assert c.get(f'/recordings/{V1}/shards/s0000/log.jsonl').status_code == 200
    assert c.get(f'/recordings/{V1}/shards/s0001/telemetry.jsonl').status_code == 200
    assert c.get(f'/recordings/{V1}/manifest.json').status_code == 200
    assert c.get(f'/recordings/{V0}/frames/000000.jpg').mimetype == 'image/jpeg'
    assert c.get(f'/recordings/{V0}/log.jsonl').status_code == 200
    assert c.get(f'/review/{V0}').status_code == 200


def test_traversal_404():
    c = _setup()
    for url in ('/recordings/..%2f..%2fetc/passwd',
                f'/recordings/{V0}/../../config.py',
                '/recordings/x/log.jsonl',
                f'/recordings/{V0}/frames/..%2f..%2fmeta.json',
                f'/recordings/{V0}/shards/s0000/frames/000000.jpg'):
        assert c.get(url).status_code == 404, url
    r = c.get(f'/recordings/{V1}/shards/s0000/../../meta.json')
    assert r.status_code < 500
    assert V0.encode() not in r.data


def test_symlink_escape_404():
    c = _setup()
    os.symlink('/etc/passwd', os.path.join(_env.REC_DIR, V0, 'frames', '000099.jpg'))
    os.symlink('/etc/passwd', os.path.join(_env.REC_DIR, V1, 'shards', 's0000', 'frames', '000099.jpg'))
    assert c.get(f'/recordings/{V0}/frames/000099.jpg').status_code == 404
    assert c.get(f'/recordings/{V1}/shards/s0000/frames/000099.jpg').status_code == 404


def test_tar_excludes_tmp():
    c = _setup()
    for n, sub in ((V0, 'frames'), (V1, 'shards/s0000/frames')):
        with open(os.path.join(_env.REC_DIR, n, sub, '.000001.jpg.tmp'), 'w') as fh:
            fh.write('x')
        with open(os.path.join(_env.REC_DIR, n, 'stray.tmp'), 'w') as fh:
            fh.write('x')
        r = c.get(f'/recordings/{n}.tar')
        assert r.status_code == 200
        names = tarfile.open(fileobj=io.BytesIO(r.data)).getnames()
        assert any(x.endswith('meta.json') for x in names), names
        assert not [x for x in names if x.endswith('.tmp')], names


def test_delete_needs_auth():
    c = _setup()
    r = c.delete(f'/api/recordings/{V0}')
    assert r.status_code == 403, r.status_code
    assert os.path.isdir(os.path.join(_env.REC_DIR, V0))


if __name__ == '__main__':
    _env.run_tests(globals())
