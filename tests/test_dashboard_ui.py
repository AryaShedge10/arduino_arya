"""Dashboard Record tab (Playwright): mode select, start/stop, disabled while recording."""
import _env
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT, WS_PORT = 5071, 5072
BASE = f'http://127.0.0.1:{PORT}/'
CTL = os.path.join(_env.DASH_DIR, 'control.json')
STATUS = os.path.join(_env.DASH_DIR, 'status.json')


def _poll(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.15)
    return None


def _atomic_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def test_record_tab_mode_select():
    import control_state
    from playwright.sync_api import sync_playwright
    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT), AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')], cwd=_env.REPO,
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try:
                return urllib.request.urlopen(BASE, timeout=1).status == 200
            except Exception:
                return False
        assert _poll(up, 20), 'web.py did not start'
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                page.on('console', lambda m: m.type == 'error' and not any(
                    k in m.text.lower() for k in ('favicon', '404', 'mjpeg', 'stream', 'websocket', 'err_'))
                    and errors.append(f'console: {m.text}'))
                page.goto(BASE, wait_until='domcontentloaded')
                page.click('#tabbtn-record')
                page.select_option('#rec-mode', 'triggered')
                page.click('#rec-start')
                st = _poll(lambda: (lambda s: s if s.get('record') else None)(control_state.read_state(CTL)))
                assert st, 'record never became True'
                assert st['record_mode'] == 'triggered', st
                # simulate the AGV reporting that it is recording
                _atomic_json(STATUS, {'rec_on': True, 'rec_session': '20261018_150129', 'rec_mode': 'triggered',
                                      'rec_format': 'v1', 'rec_ring_s': 8, 'rec_frames': 3})
                assert _poll(lambda: page.is_disabled('#rec-mode'), 8), 'select not disabled while recording'
                txt = page.inner_text('#rec-session')
                assert '20261018_150129' in txt and 'v1' in txt and 'triggered' in txt, txt
                page.click('#rec-stop')
                assert _poll(lambda: control_state.read_state(CTL)['record'] is False), 'record never became False'
                _atomic_json(STATUS, {'rec_on': False})
                assert _poll(lambda: not page.is_disabled('#rec-mode'), 8), 'select still disabled after stop'
                page.wait_for_timeout(500)
            finally:
                browser.close()
        assert not errors, errors
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


if __name__ == '__main__':
    _env.run_tests(globals())
