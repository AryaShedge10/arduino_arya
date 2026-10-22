"""Dashboard LiDAR tab (Playwright): sliders render from the server's
limits, Apply persists clamped values, and the live preview tracks the
action the AGV is actually taking."""
import _env
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT, WS_PORT = 5073, 5074
BASE = f'http://127.0.0.1:{PORT}/'
CTL = os.path.join(_env.DASH_DIR, 'control.json')
STATUS = os.path.join(_env.DASH_DIR, 'status.json')
POINTS = os.path.join(_env.DASH_DIR, 'points.json')


def _poll(fn, timeout=5.0):
    return _env.poll(fn, timeout)


def _atomic_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _points_payload(stop=0.50, slow=1.00):
    """A points.json shaped like lidar.PointsPublisher's, carrying one
    return in each zone so the canvas has all three colours to draw."""
    return {
        'updated_at': time.time(),
        'window_s': 2.0,
        'lidar_stop_dist_m': stop,
        'lidar_slow_dist_m': slow,
        'n': 3,
        'points': [
            {'t': 0.0, 'a': 0.0,  'd': stop / 2,  'zone': 'stop'},
            {'t': 0.5, 'a': 0.0,  'd': (stop + slow) / 2, 'zone': 'slow'},
            {'t': 1.0, 'a': 0.0,  'd': stop + slow + 2.0, 'zone': 'clear'},
        ],
    }


def _canvas_counts(page):
    """Count red / orange / green *point* pixels on the polar canvas.

    Points are drawn with a per-point fade (alpha down to ~0 as the trail
    ages out), so absolute RGB thresholds would make this a timing race.
    These are chromaticity ratios instead — which channel dominates, and
    by how much — so a half-faded point still classifies as its own hue.
    Red #ef4444 and orange #f59e0b are the hard pair: the fade drags both
    toward the background, and orange's blue collapses fastest, so the
    split is on g/r and on orange's g ≫ b.

    Nothing else on the canvas matches: the zone shading is 7–10 % alpha
    (far too dark), the ring/spoke stroke #233152 is blue-dominant, and
    the #94a3b8 labels have no channel dominant enough to qualify."""
    return page.evaluate("""() => {
      const c = document.getElementById('lidar-points');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      const out = {red: 0, orange: 0, green: 0, ink: 0};
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i+1], b = d[i+2];
        if (r + g + b > 66) out.ink++;              // anything not background
        if (r > 50 && g < r * 0.6 && b < r * 0.8) out.red++;
        else if (r > 50 && g > r * 0.45 && g > b * 1.6) out.orange++;
        else if (g > 80 && r < g * 0.6 && b < g * 0.8) out.green++;
      }
      return out;
    }""")


def _strip_chip(page, chip_id):
    """Read a chip out of the top summary strip: its text and its colour class."""
    return page.evaluate("""(id) => {
      const el = document.getElementById(id);
      return {v: el.querySelector('.v').textContent.trim(),
              cls: el.className.replace('chip', '').trim(),
              title: el.title};
    }""", chip_id)


def _status(**over):
    """A status.json shaped like the one the AGV publishes, so apply()
    has real LiDAR fields to render."""
    base = {
        'running': True, 'enabled': False, 'mode': 'auto',
        'obstacle_enforced': True, 'obstacle_state': 'CLEAR',
        'stm32_connected': True, 'camera_connected': True,
        'lidar_enabled': True, 'lidar_action': 'CLEAR',
        'lidar_connected': True, 'lidar_port': '/dev/agv-test-none',
        'lidar_baud': 115200,
        'lidar_min_front_m': 3.2, 'lidar_sector_mins': [3.2] * 12,
        'lidar_age_ms': 40, 'lidar_estop_reason': '',
        'lidar_stop_dist_m': 0.5, 'lidar_slow_dist_m': 1.0,
        'lidar_sector_deg': 30, 'logs': [], 'sensor_probes': [],
    }
    base.update(over)
    return base


def test_lidar_tab():
    import control_state
    from playwright.sync_api import sync_playwright

    env = dict(os.environ, AGV_DASHBOARD_PORT=str(PORT),
               AGV_DASHBOARD_WS_PORT=str(WS_PORT))
    log = open(os.path.join(_env.TMP, 'web_lidar.out'), 'w')
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
                page.goto(BASE, wait_until='domcontentloaded')

                # the tab exists and opens
                page.click('#tabbtn-lidar')
                assert _poll(lambda: page.is_visible('#lidar-card'), 5), 'lidar panel did not open'

                # one slider per knob, built from the server's limits
                for key in ('lidar_stop_dist_m', 'lidar_slow_dist_m',
                            'lidar_slow_v_mps', 'lidar_stale_ms', 'lidar_sector_deg'):
                    assert _poll(lambda k=key: page.query_selector(f'#lidar-in-{k}') is not None, 5), \
                        f'no slider for {key}'

                # the value shown matches what the server would store
                assert page.input_value('#lidar-in-lidar_stop_dist_m') == '0.5'
                assert '0.50 m' in page.inner_text('#lidar-val-lidar_stop_dist_m')

                # ── Apply persists, and the server's clamp wins ──
                page.fill('#lidar-in-lidar_stop_dist_m', '0.75')
                page.dispatch_event('#lidar-in-lidar_stop_dist_m', 'input')
                assert '0.75 m' in page.inner_text('#lidar-val-lidar_stop_dist_m')
                page.click('#lidar-apply')
                assert _poll(lambda: control_state.read_state(CTL)['lidar_stop_dist_m'] == 0.75, 5), \
                    'stop distance not persisted'
                assert _poll(lambda: 'saved' in page.inner_text('#lidar-apply-msg'), 5), \
                    'no saved confirmation'

                # the slow zone is held clear of the stop zone on disk
                st = control_state.read_state(CTL)
                assert st['lidar_slow_dist_m'] >= st['lidar_stop_dist_m'] + \
                    control_state.LIDAR_SLOW_GAP_M, st

                # ── live preview follows the AGV's reported action ──
                _atomic_json(STATUS, _status())
                assert _poll(lambda: page.inner_text('#lidar-min-front').strip() == '3.20 m', 8), \
                    f"front distance not rendered: {page.inner_text('#lidar-min-front')!r}"
                assert _poll(lambda: page.inner_text('#lidar-act-now').strip() == 'CLEAR', 8), \
                    f"action not rendered: {page.inner_text('#lidar-act-now')!r}"

                # ── the summary strip carries the LiDAR verdict too ──
                # The strip is what the operator sees without opening a
                # tab, so it must show the action the rover is taking,
                # not just that the sensor is plugged in.
                assert _poll(lambda: _strip_chip(page, 'sum-lidar')['v'] == 'CLEAR · 3.20 m', 8), \
                    f"strip lidar: {_strip_chip(page, 'sum-lidar')}"
                assert _strip_chip(page, 'sum-lidar')['cls'] == 'good'
                assert 'tty' in _strip_chip(page, 'sum-lidar')['title'] or \
                    'test-none' in _strip_chip(page, 'sum-lidar')['title']
                assert _strip_chip(page, 'sum-cam')['v'] == 'OK', \
                    f"strip camera: {_strip_chip(page, 'sum-cam')}"

                # SLOW is amber yet still a real reading, so it keeps the
                # distance — only a hard stop or a disarmed sensor drops it
                _atomic_json(STATUS, _status(lidar_action='SLOW', lidar_min_front_m=0.80))
                assert _poll(lambda: _strip_chip(page, 'sum-lidar')['v'] == 'SLOW · 0.80 m', 8), \
                    f"strip lidar: {_strip_chip(page, 'sum-lidar')}"
                assert _strip_chip(page, 'sum-lidar')['cls'] == 'warn'

                # an obstacle inside the stop zone flips the chip to ESTOP
                _atomic_json(STATUS, _status(lidar_action='ESTOP',
                                             lidar_min_front_m=0.42,
                                             lidar_sector_mins=[0.42] + [3.0] * 11,
                                             lidar_estop_reason='lidar estop front=0.42m'))
                assert _poll(lambda: page.inner_text('#lidar-act-now').strip() == 'ESTOP', 8), \
                    'ESTOP not shown'
                assert _poll(lambda: 'ESTOP' in page.inner_text('#lidar-action-chip'), 5)
                assert _poll(lambda: page.inner_text('#lidar-min-front').strip() == '0.42 m', 8)

                # the strip goes red with it, and drops the distance —
                # an out-of-range reading must never ride alongside a verdict
                assert _poll(lambda: _strip_chip(page, 'sum-lidar')['v'] == 'ESTOP', 8), \
                    f"strip lidar: {_strip_chip(page, 'sum-lidar')}"
                assert _strip_chip(page, 'sum-lidar')['cls'] == 'bad'

                # ── the polar canvas draws real points ──
                # Publish a hand-built points.json with one return in
                # each zone, then look for the pixels each zone's colour
                # should have produced. This exercises the whole chain:
                # file → fetch → zone classification → canvas.
                _atomic_json(POINTS, _points_payload())
                assert _poll(lambda: _canvas_counts(page)['red'] > 0, 8), \
                    'a point inside the stop zone was not drawn red'
                counts = _canvas_counts(page)
                assert counts['orange'] > 0, 'no point drawn in the slow zone'
                assert counts['green'] > 0, 'no point drawn in the clear zone'

                # a stale sensor is surfaced, not hidden
                _atomic_json(STATUS, _status(lidar_action='ESTOP',
                                             lidar_min_front_m=None,
                                             lidar_sector_mins=None,
                                             lidar_age_ms=99999,
                                             lidar_estop_reason='stale'))
                assert _poll(lambda: page.inner_text('#lidar-min-front').strip() == '—', 8)
                assert _poll(lambda: page.get_attribute('#lidar-age', 'title') == 'stale', 8), \
                    'estop reason not surfaced'

                # safety OFF is visibly a safety-off state
                _atomic_json(STATUS, _status(lidar_enabled=False, lidar_action='OFF'))
                assert _poll(lambda: page.inner_text('#lidar-act-now').strip() == 'OFF', 8)
                assert _poll(lambda: 'OFF' in page.inner_text('#lidar-label'), 5)

                # disarmed safety must never read as safe on the strip:
                # amber OFF, not green, and no distance beside it
                assert _poll(lambda: _strip_chip(page, 'sum-lidar')['v'] == 'OFF', 8), \
                    f"strip lidar: {_strip_chip(page, 'sum-lidar')}"
                assert _strip_chip(page, 'sum-lidar')['cls'] == 'warn'

                page.wait_for_timeout(400)
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