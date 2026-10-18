"""Review page (Playwright): v0 + v1 fixture sessions, derived views, fallback to old URLs."""
import _env
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager

import fixtures

PORT, WS_PORT = 5071, 5072
PORT_E, WS_PORT_E = 5073, 5074
BASE = f'http://127.0.0.1:{PORT}/'
BASE_E = f'http://127.0.0.1:{PORT_E}/'
V0, V1 = '20261018_120000', '20261018_130000'
NOISE = ('favicon', '404', 'mjpeg', 'stream', 'websocket', 'err_', 'aborted', 'failed to load resource')


def _poll(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(0.15)
    return None


@contextmanager
def _server(enforce=False):
    port, ws = (PORT_E, WS_PORT_E) if enforce else (PORT, WS_PORT)
    tag = 'enf' if enforce else 'plain'
    rec = os.path.join(_env.TMP, 'rec_' + tag)
    dash = os.path.join(_env.TMP, 'dash_' + tag)
    os.makedirs(rec, exist_ok=True)
    os.makedirs(dash, exist_ok=True)
    fixtures.make_v0_session(rec, V0)
    fixtures.make_v1_session(rec, V1)
    with open(os.path.join(dash, 'control.json'), 'w') as fh:
        json.dump({'enabled': False}, fh)
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(port), AGV_DASHBOARD_WS_PORT=str(ws),
               AGV_REC_DIR=rec, AGV_DASHBOARD_DIR=dash, AGV_LOG_FILE=os.path.join(dash, 'agv.log'),
               AGV_CONTROL_FILE=os.path.join(dash, 'control.json'))
    if enforce:
        env.pop('AGV_REC_REQUIRE_AUTH', None)     # default = enforced
    else:
        env['AGV_REC_REQUIRE_AUTH'] = '0'
    base = f'http://127.0.0.1:{port}/'
    log = open(os.path.join(_env.TMP, f'web_review_{tag}.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')], cwd=_env.REPO,
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            return urllib.request.urlopen(base, timeout=1).status == 200
        assert _poll(up, 20), 'web.py did not start'
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


def _page(browser, errors):
    page = browser.new_page()
    page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    page.on('console', lambda m: m.type == 'error' and not any(k in m.text.lower() for k in NOISE)
            and errors.append(f'console: {m.text}'))
    return page


def _nw(page, sel):
    return page.evaluate(f"document.querySelector('{sel}').naturalWidth")


def _check_session(page, name, v1, base=BASE):
    page.goto(f'{base}review/{name}', wait_until='domcontentloaded')
    assert _poll(lambda: re.match(r'^\d+ / \d+', page.inner_text('#pos')) and not page.inner_text('#pos').startswith('0 / 0'), 8), \
        page.inner_text('#pos')
    assert _poll(lambda: _nw(page, '#frame') > 0, 5), 'frame not loaded'
    assert _poll(lambda: _nw(page, '#viewimg') > 0, 8), 'views image not loaded'
    before = page.inner_text('#pos')
    page.keyboard.press('ArrowRight')
    assert _poll(lambda: page.inner_text('#pos') != before, 3), 'ArrowRight did not move'
    detail = page.inner_text('#detail')
    if v1:
        for label in ('rpm L', 'ticks since zero', 'frame age', 'firmware', 'luma', 'save'):
            assert label in detail, (label, detail)
        assert 'schema 1' in page.inner_text('#schemainfo')
    else:
        assert 'rpm' not in detail and 'firmware' not in detail, detail


def test_review_v0_and_v1():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            _check_session(page, V0, False)
            _check_session(page, V1, True)
            # v1 derived status line shows cache info; flagged filter counts trigger rows
            assert 'cache' in page.inner_text('#vstatus')
            page.select_option('#filter', 'flag')
            assert _poll(lambda: re.match(r'^\d+ / (\d+)', page.inner_text('#pos')) and
                         int(re.match(r'^\d+ / (\d+)', page.inner_text('#pos')).group(1)) < 12, 3)
            page.wait_for_timeout(300)
        finally:
            browser.close()
    assert not errors, errors


def test_review_derived_error_text():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            page.route('**/derived/**', lambda route: route.fulfill(
                status=503, content_type='application/json', body='{"error": "busy: rover driving"}'))
            page.goto(f'{BASE}review/{V1}', wait_until='domcontentloaded')
            assert _poll(lambda: 'busy: rover driving' in page.inner_text('#noview'), 8), page.inner_text('#noview')
        finally:
            browser.close()
    assert not errors, errors


def test_review_falls_back_when_api_404():
    from playwright.sync_api import sync_playwright
    errors = []
    seen = []
    with _server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            page.route('**/api/recordings/**', lambda route: route.fulfill(
                status=404, content_type='application/json', body='{"error":"not found"}'))
            page.on('request', lambda rq: seen.append(rq.url))
            page.goto(f'{BASE}review/{V0}', wait_until='domcontentloaded')
            assert _poll(lambda: re.match(r'^\d+ / \d+', page.inner_text('#pos')) and
                         not page.inner_text('#pos').startswith('0 / 0'), 8), page.inner_text('#pos')
            assert _poll(lambda: _nw(page, '#frame') > 0, 5)
            for _ in range(3):   # row #3 has stored views; #0 would need the (blocked) derived endpoint
                page.keyboard.press('ArrowRight')
            assert _poll(lambda: _nw(page, '#viewimg') > 0, 5), 'stored v0 views must still show'
            assert any(u.endswith(f'/recordings/{V0}/log.jsonl') for u in seen), seen
            assert 'frames' in page.inner_text('#meta')
        finally:
            browser.close()


def _login_tokens():
    req = urllib.request.Request(BASE_E + 'api/login', data=json.dumps({'password': 'testpw'}).encode(),
                                 headers={'Content-Type': 'application/json'})
    j = json.load(urllib.request.urlopen(req, timeout=5))
    now = int(time.time() * 1000)
    return {'access': j['access_token'], 'refresh': j['refresh_token'],
            'accessExp': now + j['access_expires_in'] * 1000, 'refreshExp': now + j['refresh_expires_in'] * 1000}


def _preload(page, data):
    page.add_init_script("try { localStorage.setItem('agv-auth', %s); } catch (e) {}" % json.dumps(json.dumps(data)))


def _assert_tar(path):
    import tarfile
    with tarfile.open(path) as tf:
        names = tf.getnames()
    assert any(n.endswith('log.jsonl') or n.endswith('.jpg') for n in names), names


def test_review_enforced_signin_panel_and_login():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(True), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            page.goto(f'{BASE_E}review/{V1}', wait_until='domcontentloaded')
            assert _poll(lambda: page.is_visible('#signin'), 8), 'sign-in panel not shown'
            page.wait_for_timeout(400)
            assert not page.evaluate("document.getElementById('frame').getAttribute('src')"), 'frame must not load'
            assert page.inner_text('#pos').startswith('0 / 0')
            # wrong password first, then the right one
            page.fill('#signin-pw', 'nope')
            page.click('#signin-btn')
            assert _poll(lambda: page.inner_text('#signin-err').strip(), 5), 'no error shown'
            assert page.is_visible('#signin')
            page.fill('#signin-pw', 'testpw')
            page.click('#signin-btn')
            assert _poll(lambda: not page.is_visible('#signin'), 5)
            assert _poll(lambda: re.match(r'^\d+ / \d+', page.inner_text('#pos')) and not page.inner_text('#pos').startswith('0 / 0'), 8)
            assert _poll(lambda: _nw(page, '#frame') > 0, 5)
            assert _poll(lambda: _nw(page, '#viewimg') > 0, 8)
            # v0 session in the same (signed-in) browser context
            _check_session(page, V0, False, BASE_E)
        finally:
            browser.close()
    assert not errors, errors


def test_review_enforced_preloaded_session_and_download():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(True), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            data = _login_tokens()
            page = _page(browser, errors)
            _preload(page, data)
            _check_session(page, V1, True, BASE_E)
            assert not page.is_visible('#signin')
            _check_session(page, V0, False, BASE_E)
            page.goto(f'{BASE_E}review/{V1}', wait_until='domcontentloaded')
            assert _poll(lambda: _nw(page, '#frame') > 0, 8)
            with page.expect_download(timeout=15000) as dl:
                page.click('#dl')
            path = os.path.join(_env.TMP, 'review_dl.tar')
            dl.value.save_as(path)
            _assert_tar(path)
        finally:
            browser.close()
    assert not errors, errors


def test_dashboard_download_button_enforced():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(True), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            _preload(page, _login_tokens())
            page.goto(BASE_E + '#record', wait_until='domcontentloaded')
            sel = f'button[data-dl="{V1}"]'
            assert _poll(lambda: page.query_selector(sel), 10), 'session list not loaded'
            with page.expect_download(timeout=15000) as dl:
                page.click(sel)
            path = os.path.join(_env.TMP, 'dash_dl.tar')
            dl.value.save_as(path)
            _assert_tar(path)
        finally:
            browser.close()


def test_review_enforced_garbage_session_shows_signin():
    from playwright.sync_api import sync_playwright
    errors = []
    with _server(True), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            now = int(time.time() * 1000)
            page = _page(browser, errors)
            _preload(page, {'access': 'garbage', 'refresh': 'garbage',
                            'accessExp': now + 600000, 'refreshExp': now + 3600000})
            page.goto(f'{BASE_E}review/{V1}', wait_until='domcontentloaded')
            assert _poll(lambda: page.is_visible('#signin'), 8), 'sign-in panel not shown'
            assert not page.evaluate("document.getElementById('frame').getAttribute('src')")
        finally:
            browser.close()


def test_review_plain_sends_no_auth():
    from playwright.sync_api import sync_playwright
    errors, reqs = [], []
    with _server(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, errors)
            page.on('request', lambda rq: reqs.append((rq.url, rq.headers.get('authorization'))))
            _check_session(page, V1, True)
            assert not page.is_visible('#signin')
            assert not [r for r in reqs if re.search(r'/api/recordings/[^/]+/token', r[0])], reqs
            assert not [r for r in reqs if r[1]], [r for r in reqs if r[1]]
            assert not [r for r in reqs if 'ft=' in r[0]], reqs
        finally:
            browser.close()
    assert not errors, errors


if __name__ == '__main__':
    _env.run_tests(globals())
