"""GET/POST /api/lidar — clamps, auth, and the invariants the rover relies on.

The stop distance decides how hard the AGV brakes, so these tests are as
much about "the server cannot be talked into a bad value" as about the
happy path.
"""
import _env  # noqa: F401  (must be first)
import json
import os

import control_state
import web


def _client():
    return web.flask_app.test_client()


def _auth():
    # _password_ok takes a real access token under Authorization, or the
    # raw password under X-AGV-Password (the header scripts use).
    return {'X-AGV-Password': 'testpw'}


def _reset():
    """Back to factory defaults so tests don't inherit each other's writes."""
    p = control_state.CONTROL_PATH
    if os.path.exists(p):
        os.unlink(p)


def _get(c):
    r = c.get('/api/lidar')
    assert r.status_code == 200, r.status_code
    return r.get_json()


# ── GET ─────────────────────────────────────────────────────────────

def test_get_returns_current_settings():
    _reset()
    d = _get(_client())
    assert d['lidar_enabled'] is True
    assert d['lidar_stop_dist_m'] == 0.50
    assert d['lidar_slow_dist_m'] == 1.00
    assert d['lidar_slow_v_mps'] == 0.20
    assert d['lidar_stale_ms'] == 200
    assert d['lidar_sector_deg'] == 30


def test_get_includes_limits_for_the_sliders():
    """The dashboard builds its sliders from this, so every knob the tab
    renders must appear here."""
    d = _get(_client())
    for key in control_state.LIDAR_CLAMPS:
        assert key in d, key
        assert d['limits'][key]['min'] <= d['limits'][key]['max']
        assert d['limits'][key]['step'] > 0
    assert d['slow_gap_m'] == control_state.LIDAR_SLOW_GAP_M
    assert d['limits']['lidar_sector_deg']['choices'] == \
        list(control_state.LIDAR_SECTOR_CHOICES)


def test_get_needs_no_password():
    """Reading the zones is not privileged — the dashboard polls it on
    load, before the operator has signed in."""
    assert _client().get('/api/lidar').status_code == 200


# ── POST: happy path ────────────────────────────────────────────────

def test_post_sets_values():
    _reset()
    c = _client()
    r = c.post('/api/lidar', json={'lidar_stop_dist_m': 0.70,
                                  'lidar_slow_dist_m': 1.60,
                                  'lidar_slow_v_mps': 0.18,
                                  'lidar_stale_ms': 350,
                                  'lidar_sector_deg': 45})
    assert r.status_code == 200, r.status_code
    d = r.get_json()
    assert d['lidar_stop_dist_m'] == 0.70
    assert d['lidar_slow_dist_m'] == 1.60
    assert d['lidar_stale_ms'] == 350
    assert d['lidar_sector_deg'] == 45
    # and it really landed in control.json, which is what the AGV reads
    on_disk = json.load(open(control_state.CONTROL_PATH))
    assert on_disk['lidar_stop_dist_m'] == 0.70
    assert on_disk['lidar_sector_deg'] == 45


def test_post_partial_update_keeps_the_rest():
    _reset()
    c = _client()
    c.post('/api/lidar', json={'lidar_stop_dist_m': 0.8,
                               'lidar_slow_dist_m': 1.8,
                               'lidar_stale_ms': 300})
    d = c.post('/api/lidar', json={'lidar_slow_v_mps': 0.12}).get_json()
    assert d['lidar_slow_v_mps'] == 0.12
    assert d['lidar_stop_dist_m'] == 0.8        # untouched
    assert d['lidar_stale_ms'] == 300          # untouched


def test_post_empty_body_is_a_no_op():
    _reset()
    c = _client()
    before = c.get('/api/lidar').get_json()
    assert c.post('/api/lidar', json={}).status_code == 200
    after = c.get('/api/lidar').get_json()
    assert {k: after[k] for k in control_state.LIDAR_CLAMPS} == \
           {k: before[k] for k in control_state.LIDAR_CLAMPS}


# ── POST: server-side clamping ──────────────────────────────────────

def test_post_clamps_out_of_range():
    """The exact case from the plan's verification list."""
    _reset()
    c = _client()
    d = c.post('/api/lidar', json={'lidar_stop_dist_m': 5.0}).get_json()
    assert d['lidar_stop_dist_m'] == control_state.LIDAR_STOP_MAX_M
    assert json.load(open(control_state.CONTROL_PATH))['lidar_stop_dist_m'] == \
        control_state.LIDAR_STOP_MAX_M


def test_post_clamps_every_knob():
    _reset()
    d = _client().post('/api/lidar', json={
        'lidar_stop_dist_m': -1, 'lidar_slow_dist_m': 99,
        'lidar_slow_v_mps': 0, 'lidar_stale_ms': 99999,
        'lidar_sector_deg': 2,
    }).get_json()
    assert d['lidar_stop_dist_m'] == control_state.LIDAR_STOP_MIN_M
    assert d['lidar_slow_dist_m'] == control_state.LIDAR_SLOW_MAX_M
    assert d['lidar_slow_v_mps'] == control_state.LIDAR_SLOW_V_MIN
    assert d['lidar_stale_ms'] == control_state.LIDAR_STALE_MAX_MS
    assert d['lidar_sector_deg'] == control_state.LIDAR_SECTOR_MIN_DEG


def test_post_rejects_non_numeric():
    _reset()
    c = _client()
    before = c.get('/api/lidar').get_json()
    r = c.post('/api/lidar', json={'lidar_stop_dist_m': 'far'})
    assert r.status_code == 400, r.status_code
    # a rejected request must not have written anything
    assert c.get('/api/lidar').get_json()['lidar_stop_dist_m'] == \
        before['lidar_stop_dist_m']


def test_post_accepts_numeric_strings():
    """HTML range inputs and hand-written curl both send strings."""
    _reset()
    d = _client().post('/api/lidar', json={'lidar_stop_dist_m': '0.65'}).get_json()
    assert d['lidar_stop_dist_m'] == 0.65


# ── POST: invariants ────────────────────────────────────────────────

def test_slow_zone_pushed_above_stop():
    """Sending slow *below* stop must not leave the rover crawling inside
    the distance it is supposed to stop at."""
    _reset()
    d = _client().post('/api/lidar', json={'lidar_stop_dist_m': 0.90,
                                           'lidar_slow_dist_m': 0.40}).get_json()
    assert d['lidar_slow_dist_m'] >= d['lidar_stop_dist_m'] + control_state.LIDAR_SLOW_GAP_M


def test_raising_stop_alone_lifts_slow_too():
    _reset()
    c = _client()
    c.post('/api/lidar', json={'lidar_stop_dist_m': 0.30, 'lidar_slow_dist_m': 0.40})
    d = c.post('/api/lidar', json={'lidar_stop_dist_m': 1.10}).get_json()
    assert d['lidar_slow_dist_m'] >= 1.10 + control_state.LIDAR_SLOW_GAP_M


# ── auth: disabling needs the password, enabling never does ─────────

def test_disable_without_password_is_refused():
    _reset()
    r = _client().post('/api/lidar', json={'enabled': False})
    assert r.status_code in (401, 403), r.status_code
    # safety must still be on
    assert _get(_client())['lidar_enabled'] is True


def test_disable_with_password_works():
    _reset()
    r = _client().post('/api/lidar', json={'enabled': False}, headers=_auth())
    assert r.status_code == 200, r.status_code
    assert r.get_json()['lidar_enabled'] is False
    assert json.load(open(control_state.CONTROL_PATH))['lidar_enabled'] is False


def test_reenable_never_needs_password():
    _reset()
    c = _client()
    c.post('/api/lidar', json={'enabled': False}, headers=_auth())
    r = c.post('/api/lidar', json={'enabled': True})
    assert r.status_code == 200, r.status_code
    assert r.get_json()['lidar_enabled'] is True


def test_tuning_knobs_need_no_password():
    """Changing *how* it stops is the operator's normal work; only
    switching the layer off is privileged."""
    _reset()
    r = _client().post('/api/lidar', json={'lidar_stop_dist_m': 0.45})
    assert r.status_code == 200, r.status_code


if __name__ == '__main__':
    _env.run_tests(globals())