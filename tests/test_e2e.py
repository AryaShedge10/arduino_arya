"""End-to-end test (SLOW, ~1 minute): real web.py + real agv_drive_arduino.py subprocesses.

Fake camera, non-existent Arduino port, isolated temp dirs, ports 5071/5072 only.
Records a triggered v1 session with auth enforced, then checks files, the
tooling and the web API. Run through ``python3 tests/run_all.py --e2e``
(run_all skips this file otherwise) or directly: ``python3 tests/test_e2e.py``.
"""
import _env
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

import recording_io
import recording_tool

PORT, WS_PORT = 5071, 5072
BASE = f'http://127.0.0.1:{PORT}'


def _poll(fn, timeout=5.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    return None


def _req(path, method='GET', body=None, headers=None, raw=False):
    """Returns (status, body, content_type); HTTP errors do not raise."""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h['Content-Type'] = 'application/json'
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            b = resp.read()
            ct = resp.headers.get('Content-Type', '')
            return resp.status, (b if raw or 'json' not in ct else json.loads(b)), ct
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, (b if raw else json.loads(b)), e.headers.get('Content-Type', '')
        except ValueError:
            return e.code, b, e.headers.get('Content-Type', '')


def _stop(p, name):
    if p.poll() is not None:
        return
    p.send_signal(signal.SIGINT)
    try:
        p.wait(10)
        return
    except subprocess.TimeoutExpired:
        pass
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _env_vars():
    env = dict(os.environ)
    for k in ('AGV_REC_FORMAT', 'AGV_REC_REQUIRE_AUTH'):
        env.pop(k, None)          # exercise the config DEFAULTS (v1, auth on)
    env.update(AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT),
               AGV_REC_SHARD_S='10')
    return env


def test_end_to_end():
    from playwright.sync_api import sync_playwright
    import replay_recording
    log_path = os.environ['AGV_LOG_FILE']
    env = _env_vars()
    outs = [open(os.path.join(_env.TMP, n), 'w') for n in ('e2e_web.out', 'e2e_agv.out')]
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')], cwd=_env.REPO,
                                      env=env, stdout=outs[0], stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'agv_drive_arduino.py')],
                                      cwd=_env.REPO, env=env, stdout=outs[1], stderr=subprocess.STDOUT))
        assert _poll(lambda: _req('/')[0] == 200, 25), 'web.py did not start'
        # AGV up: its status must report running
        assert _poll(lambda: procs[1].poll() is None and _req('/status')[1].get('running'), 25), \
            'AGV not running: ' + open(outs[1].name).read()[-800:]

        st, j, _ = _req('/api/login', 'POST', {'password': 'testpw'})
        assert st == 200 and j.get('access_token'), (st, j)
        bearer = {'Authorization': 'Bearer ' + j['access_token']}
        login = j

        st, j, _ = _req('/api/record', 'POST', {'action': 'start', 'rate': 5, 'mode': 'triggered'})
        assert st == 200, (st, j)
        t0 = time.time()
        sess = _poll(lambda: [s for s in _req('/api/recordings')[1]['sessions']][:1] or None, 15)
        assert sess, 'no session appeared'
        name = sess[0]['name']
        folder = os.path.join(_env.REC_DIR, name)
        # auth is enforced once a session exists
        st, _, _ = _req(f'/api/recordings/{name}/meta')
        assert st == 401, st
        st, _, _ = _req(f'/api/recordings/{name}/meta', headers=bearer)
        assert st == 200, st

        time.sleep(max(0, 14 - (time.time() - t0)))
        st, j, _ = _req('/api/record', 'POST', {'action': 'mark', 'note': 'e2e mark'})
        assert st == 200, (st, j)
        time.sleep(5)
        st, j, _ = _req('/api/record', 'POST', {'action': 'stop'})
        assert st == 200, (st, j)
        assert _poll(lambda: os.path.exists(os.path.join(folder, 'summary.json')), 15), 'no summary.json'

        names = [s['name'] for s in _req('/api/recordings')[1]['sessions']]
        assert names == [name], names

        # ---- files
        assert recording_tool.main(['verify', folder]) == 0
        man = json.load(open(os.path.join(folder, 'manifest.json')))
        assert len(man.get('shards', [])) >= 2, man.keys()
        assert json.load(open(os.path.join(folder, 'summary.json'))).get('complete') is True
        assert json.load(open(os.path.join(folder, 'meta.json'))).get('schema') == 1
        rows = recording_io.load_rows(folder)
        assert rows, 'no rows'
        idx = [r['i'] for r in rows]
        assert idx == sorted(idx), 'rows not sorted by i'
        saves = {r.get('save') for r in rows}
        assert 'mark' in saves, saves
        assert 'baseline' in saves, saves
        # Without an Arduino 'link_down' is persistent; it now re-fires only every
        # REC_TRIG_PERSIST_S, so 'pre' rows may or may not appear (not asserted here).
        assert 'trigger' in saves and 'post' in saves, saves
        assert any('link_down' in (r.get('triggers') or []) for r in rows if r.get('save') == 'trigger')
        tl = [os.path.join(dp, 'telemetry.jsonl') for dp, _, fs in os.walk(os.path.join(folder, 'shards'))
              if 'telemetry.jsonl' in fs]
        assert sum(1 for p in tl for l in open(p) if l.strip()) > 0, 'no telemetry'

        old = sys.argv
        sys.argv = ['replay_recording.py', folder]
        try:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                assert replay_recording.main() == 0
        finally:
            sys.argv = old

        # ---- web API
        st, body, _ = _req(f'/api/recordings/{name}/log.jsonl', headers=bearer, raw=True)
        assert st == 200
        assert len([l for l in body.decode().splitlines() if l.strip()]) == len(rows)
        st, j, _ = _req(f'/api/recordings/{name}/token', 'POST', headers=bearer)
        assert st == 200 and j.get('ft'), (st, j)
        ft = j['ft']
        row = next(r for r in rows if recording_io.frame_path(folder, r))
        rel = os.path.relpath(recording_io.frame_path(folder, row), os.path.realpath(folder))
        st, body, ct = _req(f'/recordings/{name}/{rel}?ft={ft}', raw=True)
        assert st == 200 and ct.startswith('image/jpeg') and body[:2] == b'\xff\xd8', (st, ct)
        st, _, _ = _req(f'/recordings/{name}/{rel}')
        assert st == 401, st
        st, _, ct = _req(f'/api/recordings/{name}/derived/{row["i"]}.jpg?kind=views&ft={ft}', raw=True)
        assert st == 200, st
        st, body, _ = _req(f'/recordings/{name}.tar?ft={ft}', raw=True)
        assert st == 200
        with tempfile.NamedTemporaryFile(suffix='.tar') as tf:
            tf.write(body)
            tf.flush()
            with tarfile.open(tf.name) as t:
                tn = t.getnames()
        assert any(n.endswith('manifest.json') for n in tn) and any(n.endswith('.jpg') for n in tn)

        # ---- review page
        now = int(time.time() * 1000)
        auth = {'access': login['access_token'], 'refresh': login['refresh_token'],
                'accessExp': now + login['access_expires_in'] * 1000,
                'refreshExp': now + login['refresh_expires_in'] * 1000}
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.add_init_script("try { localStorage.setItem('agv-auth', %s); } catch (e) {}"
                                     % json.dumps(json.dumps(auth)))
                page.goto(f'{BASE}/review/{name}', wait_until='domcontentloaded')
                import re
                assert _poll(lambda: re.match(r'^[1-9]\d* / \d+', page.inner_text('#pos')), 10), page.inner_text('#pos')
                assert _poll(lambda: page.evaluate("document.querySelector('#frame').naturalWidth") > 0, 8), \
                    'review frame not loaded'
            finally:
                browser.close()
    finally:
        for p in reversed(procs):
            _stop(p, '')
        for o in outs:
            o.close()
    for p in procs:
        assert p.poll() is not None, 'process left running'
    text = open(log_path, errors='replace').read()
    assert 'Cannot connect Arduino' in text, text[-1500:]
    assert 'Traceback' not in text, text[-2500:]
    for o in outs:
        assert 'Traceback' not in open(o.name, errors='replace').read(), open(o.name).read()[-2500:]


if __name__ == '__main__':
    _env.run_tests(globals())
