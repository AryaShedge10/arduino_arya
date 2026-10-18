"""T18: auth backend for recordings (file tokens, side-effect free guard).

Config is read at import time, so each scenario runs in its own subprocess
(env AGV_T18_CASE). Running this file without it runs all cases and sums them.
"""
import _env  # noqa: F401  (must be first)
import os
import subprocess
import sys
import time

CASE = os.environ.get('AGV_T18_CASE', '')
if CASE:   # set extra env AFTER _env and BEFORE importing config/web
    os.environ['AGV_REC_REQUIRE_AUTH'] = '0' if CASE == 'off' else '1'
    if CASE == 'list':
        os.environ['AGV_REC_LIST_REQUIRE_AUTH'] = '1'
    if CASE == 'ttl':
        os.environ['AGV_REC_FILE_TOKEN_TTL_S'] = '1'

from fixtures import make_v0_session, make_v1_session  # noqa: E402

A, B = '20261018_120000', '20261018_130000'
PW = {'X-AGV-Password': 'testpw'}
_s = {}


def _c():
    if not _s:
        import web
        make_v0_session(_env.REC_DIR, A, 6)
        make_v1_session(_env.REC_DIR, B, 6)
        _s['web'] = web
        _s['c'] = web.flask_app.test_client()
    return _s['c']


def _urls(n):
    return [f'/recordings/{n}/frames/000000.jpg', f'/recordings/{n}.tar',
            f'/api/recordings/{n}/log.jsonl', f'/api/recordings/{n}/events.jsonl',
            f'/api/recordings/{n}/meta', f'/api/recordings/{n}/derived/0.jpg?kind=views',
            '/api/build']


def _login():
    c = _c()
    r = c.post('/api/login', json={'password': 'testpw'})
    assert r.status_code == 200
    return r.get_json()


def _bearer(tok):
    return {'Authorization': 'Bearer ' + tok}


def _ft(n, headers):
    r = _c().post(f'/api/recordings/{n}/token', headers=headers)
    assert r.status_code == 200, r.data
    j = r.get_json()
    assert j['expires_in'] > 0
    return j['ft']


# ---- enforced ----
def test_enforced_no_credentials_401_not_counted():
    c = _c()
    web = _s['web']
    before = len(web._fail_times)
    for u in _urls(A) + _urls(B):
        assert c.get(u).status_code == 401, u
    assert c.get('/recordings/20990101_000000/frames/000000.jpg').status_code == 401
    for _ in range(10):
        assert c.get(_urls(A)[0]).status_code == 401
    assert len(web._fail_times) == before
    r = c.post('/api/enable', json={'enabled': True}, headers=PW)
    assert r.status_code == 200, r.data
    c.post('/api/enable', json={'enabled': False}, headers=PW)


def test_enforced_password_header():
    c = _c()
    web = _s['web']
    before = len(web._fail_times)
    assert c.get(_urls(A)[0], headers={'X-AGV-Password': 'nope'}).status_code == 401
    assert len(web._fail_times) == before
    for u in _urls(A)[:5] + _urls(B)[1:5]:
        assert c.get(u, headers=PW).status_code == 200, u
    assert c.get('/api/build', headers=PW).status_code == 200


def test_enforced_bearer_and_token_endpoint():
    c = _c()
    s = _login()
    h = _bearer(s['access_token'])
    assert c.get(_urls(A)[2], headers=h).status_code == 200
    assert c.get(_urls(A)[0], headers=_bearer('garbage')).status_code == 401
    assert c.post(f'/api/recordings/{A}/token').status_code in (401, 403)
    assert c.post(f'/api/recordings/{A}/token', headers={'X-AGV-Password': 'bad'}).status_code == 403
    assert c.post('/api/recordings/20990101_000000/token', headers=h).status_code == 404
    assert c.post(f'/api/recordings/{A}/token', headers=PW).status_code == 200
    # refresh/access tokens are not file tokens and vice versa
    assert c.get(_urls(A)[0] + '?ft=' + s['access_token']).status_code == 401
    assert c.get('/api/auth', headers=_bearer(_ft(A, h))).status_code == 401


def test_enforced_ft_works_and_is_per_session():
    c = _c()
    h = _bearer(_login()['access_token'])
    ft = _ft(A, h)
    for u in (_urls(A)[0], _urls(A)[1], _urls(A)[2], _urls(A)[4]):
        sep = '&' if '?' in u else '?'
        assert c.get(f'{u}{sep}ft={ft}').status_code == 200, u
    assert c.get(f'{_urls(B)[0]}?ft={ft}').status_code == 401
    assert c.get(f'{_urls(B)[1]}?ft={ft}').status_code == 401
    assert c.get(f'/api/build?ft={ft}').status_code == 200
    assert c.get(_urls(A)[0] + '?ft=' + ft[:-3] + 'abc').status_code == 401


def test_enforced_logout_revokes_ft():
    c = _c()
    s = _login()
    ft = _ft(A, _bearer(s['access_token']))
    assert c.get(f'{_urls(A)[0]}?ft={ft}').status_code == 200
    assert c.post('/api/logout', json={'refresh_token': s['refresh_token']}).status_code == 200
    assert c.get(f'{_urls(A)[0]}?ft={ft}').status_code == 401
    assert c.get(_urls(A)[0], headers=_bearer(s['access_token'])).status_code == 401


def test_enforced_pw_ft_and_open_shell_and_delete():
    c = _c()
    ft = _ft(A, PW)
    assert c.get(f'{_urls(A)[0]}?ft={ft}').status_code == 200
    assert c.get(f'/review/{A}').status_code == 200
    assert c.get('/api/recordings').status_code == 200          # list open
    assert c.delete(f'/api/recordings/{B}').status_code == 403   # still needs auth
    assert c.delete(f'/api/recordings/{B}', headers={'X-AGV-Password': 'x'}).status_code == 403


# ---- ttl ----
def test_ttl_ft_expires():
    c = _c()
    ft = _ft(A, PW)
    assert c.get(f'{_urls(A)[0]}?ft={ft}').status_code == 200
    time.sleep(2.1)
    assert c.get(f'{_urls(A)[0]}?ft={ft}').status_code == 401


# ---- list protected ----
def test_list_requires_auth_when_enabled():
    c = _c()
    web = _s['web']
    before = len(web._fail_times)
    assert c.get('/api/recordings').status_code == 401
    assert len(web._fail_times) == before
    assert c.get('/api/recordings', headers=PW).status_code == 200
    assert c.get('/api/recordings', headers=_bearer(_login()['access_token'])).status_code == 200
    assert c.get(f'/review/{A}').status_code == 200


# ---- off (default) ----
def test_off_everything_open():
    c = _c()
    for u in _urls(A)[:5] + _urls(B)[1:5] + ['/api/build', '/api/recordings']:
        assert c.get(u).status_code == 200, u
    assert c.get(f'/review/{A}').status_code == 200
    assert c.delete(f'/api/recordings/{B}').status_code == 403
    assert c.post(f'/api/recordings/{A}/token').status_code in (401, 403)
    assert c.post(f'/api/recordings/{A}/token', headers=PW).status_code == 200


def _select(prefix):
    return {k: v for k, v in globals().items() if k.startswith(prefix)}


if __name__ == '__main__':
    if CASE:
        pre = {'on': 'test_enforced_', 'ttl': 'test_ttl_', 'list': 'test_list_', 'off': 'test_off_'}[CASE]
        _env.run_tests(_select(pre))
    else:
        total = 0
        for case in ('on', 'ttl', 'list', 'off'):
            env = dict(os.environ, AGV_T18_CASE=case)
            for k in ('AGV_REC_DIR', 'AGV_DASHBOARD_DIR', 'AGV_LOG_FILE', 'AGV_CONTROL_FILE'):
                env.pop(k, None)
            out = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env,
                                 capture_output=True, text=True, timeout=180)
            print(out.stdout + out.stderr, end='')
            last = out.stdout.strip().splitlines()[-1:] or ['']
            if out.returncode != 0 or not last[0].startswith('OK: '):
                print(f'FAILED case {case}')
                sys.exit(1)
            total += int(last[0].split()[1])
        print(f'OK: {total} tests')
