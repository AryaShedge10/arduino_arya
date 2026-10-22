"""Dashboard summary strip: both cameras, not one (Playwright).

The strip is what the operator sees without opening a tab, and there are
two cameras to report on: the AGV pipeline's own sensor (sum-cam, fed by
status.json) and the USB C270 preview (sum-usbcam, fed by /cam/status).
They fail independently — a webcam can be unplugged while the line follower
keeps running — so each needs its own chip.

/cam/status is stubbed with a Playwright route, so this runs with no camera
plugged in and also covers the case the C270 tab is closed: the strip must
still track the camera, since that is when the operator is looking at it.
"""
import _env
import json
import os
import subprocess
import sys
import urllib.request

PORT, WS_PORT = 5075, 5076
BASE = f'http://127.0.0.1:{PORT}/'
STATUS = os.path.join(_env.DASH_DIR, 'status.json')

_LIVE = {'connected': True, 'fps': 11.8, 'age_ms': 90, 'frame_seq': 412,
         'device': '/dev/video1', 'width': 640, 'height': 480,
         'last_error': None}
_STALE = dict(_LIVE, fps=0.0, age_ms=5200, last_error=None)
_GONE = {'connected': False, 'fps': 0.0, 'age_ms': None, 'frame_seq': 412,
         'device': '/dev/video1', 'width': 0, 'height': 0,
         'last_error': 'device busy: /dev/video1'}


def _poll(fn, timeout=8.0):
    return _env.poll(fn, timeout)


def _strip_chip(page, chip_id):
    """Read a chip out of the top summary strip: its text and its colour class."""
    return page.evaluate("""(id) => {
      const el = document.getElementById(id);
      return {v: el.querySelector('.v').textContent.trim(),
              cls: el.className.replace('chip', '').trim(),
              title: el.title};
    }""", chip_id)


def _atomic_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _status(**over):
    """A status.json shaped like the one the AGV publishes, with the
    pipeline camera up."""
    base = {'running': True, 'enabled': False, 'mode': 'auto',
            'obstacle_enforced': True, 'obstacle_state': 'CLEAR',
            'stm32_connected': True, 'camera_connected': True,
            'camera_backend': 'argus', 'camera_direction': 'ON_LINE'}
    base.update(over)
    return base


def test_both_cameras_in_strip():
    from playwright.sync_api import sync_playwright

    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT),
               AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_cam.out'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'web.py')],
                            cwd=_env.REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
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
                    k in m.text.lower() for k in ('favicon', '404', 'mjpeg', 'stream', 'websocket'))
                    and errors.append(f'console: {m.text}'))

                # Stub the webcam status; the real one would report whatever
                # is (not) plugged in, which is not what this test is about.
                payload = dict(_LIVE)
                page.route('**/cam/status', lambda route: route.fulfill(
                    status=200, content_type='application/json',
                    body=json.dumps(payload)))

                page.goto(BASE, wait_until='domcontentloaded')

                # ── both chips are on the strip ──
                assert page.query_selector('#sum-cam') is not None, 'no AGV camera chip'
                assert page.query_selector('#sum-usbcam') is not None, 'no USB camera chip'

                # ── the USB camera is tracked with its tab closed ──
                # Nothing has clicked #tabbtn-cam, so the panel is hidden.
                # The strip is what the operator reads from another tab, so
                # a poll that stops when the tab hides would strand it.
                assert page.is_hidden('#tab-cam')
                assert _poll(lambda: _strip_chip(page, 'sum-usbcam')['v'] == 'OK', 8), \
                    f"strip usb cam (live): {_strip_chip(page, 'sum-usbcam')}"
                assert _strip_chip(page, 'sum-usbcam')['cls'] == 'good'
                assert '/dev/video1' in _strip_chip(page, 'sum-usbcam')['title'] and \
                       '11.8 fps' in _strip_chip(page, 'sum-usbcam')['title'], \
                    f"usb cam tooltip: {_strip_chip(page, 'sum-usbcam')['title']!r}"

                # a plugged-in camera that stopped delivering frames is not
                # "OK" — stale, and amber, so it never reads as healthy
                payload.clear(); payload.update(_STALE)
                assert _poll(lambda: _strip_chip(page, 'sum-usbcam')['v'] == 'STALE', 8), \
                    f"strip usb cam (stale): {_strip_chip(page, 'sum-usbcam')}"
                assert _strip_chip(page, 'sum-usbcam')['cls'] == 'warn'

                # unplugged: red, with the error the publisher reported
                payload.clear(); payload.update(_GONE)
                assert _poll(lambda: _strip_chip(page, 'sum-usbcam')['v'] == 'NONE', 8), \
                    f"strip usb cam (gone): {_strip_chip(page, 'sum-usbcam')}"
                assert _strip_chip(page, 'sum-usbcam')['cls'] == 'bad'
                assert 'device busy' in _strip_chip(page, 'sum-usbcam')['title']

                # ── the two chips move independently ──
                # The webcam is still gone, but the AGV camera comes up.
                _atomic_json(STATUS, _status())
                assert _poll(lambda: _strip_chip(page, 'sum-cam')['v'] == 'OK', 8), \
                    f"strip agv cam: {_strip_chip(page, 'sum-cam')}"
                assert _strip_chip(page, 'sum-cam')['cls'] == 'good'
                assert _strip_chip(page, 'sum-usbcam')['v'] == 'NONE'
                # 'OK' on a chip with no sensor named is ambiguous — the
                # tooltip has to say which camera it is talking about.
                assert 'AGV camera' in _strip_chip(page, 'sum-cam')['title'] and \
                       'argus' in _strip_chip(page, 'sum-cam')['title'], \
                    f"agv cam tooltip: {_strip_chip(page, 'sum-cam')['title']!r}"

                # ... and the other way round: pipeline camera down, webcam up
                payload.clear(); payload.update(_LIVE)
                _atomic_json(STATUS, _status(camera_connected=False))
                assert _poll(lambda: _strip_chip(page, 'sum-cam')['v'] == 'NONE', 8), \
                    f"strip agv cam (down): {_strip_chip(page, 'sum-cam')}"
                assert _strip_chip(page, 'sum-cam')['cls'] == 'bad'
                assert _poll(lambda: _strip_chip(page, 'sum-usbcam')['v'] == 'OK', 8), \
                    f"strip usb cam (back): {_strip_chip(page, 'sum-usbcam')}"

                assert not errors, errors
            finally:
                browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == '__main__':
    import _env
    _env.run_tests(dict(globals()))