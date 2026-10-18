"""T16: derived_views + /api/recordings/<n>/derived/<i>.jpg + /api/build."""
import _env  # noqa: F401  (must be first)
import os

import cv2
import numpy as np

from fixtures import make_v0_session, make_v1_session, synthetic_frame

V0, V1 = '20261018_140000', '20261018_150000'
_state = {}


def _setup():
    if _state:
        return _state['c']
    import web
    assert os.path.realpath(str(web.REC_DIR)) == os.path.realpath(_env.REC_DIR)
    make_v0_session(_env.REC_DIR, V0, 10)
    make_v1_session(_env.REC_DIR, V1, 12)
    _state['c'] = web.flask_app.test_client()
    _state['web'] = web
    return _state['c']


def test_render_views_shape_and_decode():
    import derived_views
    f = synthetic_frame(3, 640, 360)
    out = derived_views.render_views(f, warm_frames=[synthetic_frame(2, 640, 360)])
    assert out.shape == (560, 1280, 3) and out.dtype == np.uint8
    ok, buf = cv2.imencode('.jpg', out)
    assert ok and cv2.imdecode(buf, cv2.IMREAD_COLOR).shape == out.shape
    cold = derived_views.render_views(f)
    assert cold.shape == out.shape


def test_render_annotated_and_invalid():
    import derived_views
    f = synthetic_frame(1, 640, 360)
    assert derived_views.render_annotated(f).shape == f.shape
    for bad in (None, 'x', np.zeros((4, 4), np.uint8)):
        try:
            derived_views.render_views(bad)
        except ValueError:
            continue
        raise AssertionError('expected ValueError')


def test_endpoint_v1_and_v0_cache():
    c = _setup()
    for n in (V0, V1):
        i = 4
        r = c.get(f'/api/recordings/{n}/derived/{i}.jpg')
        assert r.status_code == 200 and r.mimetype == 'image/jpeg', (n, r.status_code)
        assert r.headers['X-Derived-Code'] and r.headers['X-Cache'] == 'miss'
        assert cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_COLOR).shape == (560, 1280, 3)
        r2 = c.get(f'/api/recordings/{n}/derived/{i}.jpg')
        assert r2.status_code == 200 and r2.headers['X-Cache'] == 'hit' and r2.data == r.data
    r = c.get(f'/api/recordings/{V0}/derived/1.jpg?kind=annotated')
    assert r.status_code == 200 and r.mimetype == 'image/jpeg'


def test_endpoint_errors_and_warm():
    c = _setup()
    assert c.get(f'/api/recordings/{V1}/derived/9999.jpg').status_code == 404
    assert c.get('/api/recordings/20200101_000000/derived/1.jpg').status_code == 404
    assert c.get(f'/api/recordings/{V1}/derived/2.jpg?kind=bogus').status_code == 400
    assert c.get(f'/api/recordings/{V1}/derived/2.jpg?warm=abc').status_code == 400
    r = c.get(f'/api/recordings/{V1}/derived/8.jpg?warm=99')
    assert r.status_code == 200, r.status_code


def test_refuses_while_driving():
    c = _setup()
    import control_state
    assert os.path.realpath(control_state.CONTROL_PATH).startswith(os.path.realpath(_env.TMP))
    control_state.write_state(enabled=True, path=control_state.CONTROL_PATH)
    try:
        r = c.get(f'/api/recordings/{V1}/derived/5.jpg')
        assert r.status_code == 503 and 'driving' in r.data.decode(), r.data
    finally:
        control_state.write_state(enabled=False, path=control_state.CONTROL_PATH)
    assert c.get(f'/api/recordings/{V1}/derived/5.jpg').status_code == 200


def test_build_endpoint():
    c = _setup()
    r = c.get('/api/build')
    assert r.status_code == 200
    assert set(r.get_json()) == {'git_commit', 'git_dirty', 'src_fingerprint'}


def test_semaphore_timeout_503():
    c = _setup()
    web = _state['web']
    old = web._DERIVED_ACQUIRE_S
    web._DERIVED_ACQUIRE_S = 0.2
    assert web._DERIVED_LOCK.acquire(timeout=1)
    try:
        r = c.get(f'/api/recordings/{V1}/derived/10.jpg?warm=1')   # not cached yet
        assert r.status_code == 503 and r.get_json()['error'] == 'busy', r.data
    finally:
        web._DERIVED_LOCK.release()
        web._DERIVED_ACQUIRE_S = old


if __name__ == '__main__':
    _env.run_tests(globals())
