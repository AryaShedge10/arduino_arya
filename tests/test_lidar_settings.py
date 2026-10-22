"""LiDAR zone settings in control.json: clamps, invariants, persistence.

The rover's stop distance is a safety parameter, so every value here is
clamped on the way *in* (write_state) and again on the way *out*
(read_state) — a hand-edited or truncated control.json must never be able
to widen the zones past what the hardware can react to.
"""
import _env  # noqa: F401  (must be first)
import json
import os

import control_state as cs


def _tmp(name):
    d = os.path.join(_env.TMP, name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'control.json')


def _defaults():
    p = _tmp('lidar_defaults')
    if os.path.exists(p):
        os.unlink(p)
    return cs.read_state(p)


# ── defaults ────────────────────────────────────────────────────────

def test_lidar_defaults_present():
    """A missing file yields the config defaults, not a crash."""
    st = _defaults()
    for key in ('lidar_enabled', 'lidar_stop_dist_m', 'lidar_slow_dist_m',
                'lidar_slow_v_mps', 'lidar_stale_ms', 'lidar_sector_deg'):
        assert key in st, key
    assert st['lidar_enabled'] is True
    assert st['lidar_stop_dist_m'] == 0.50
    assert st['lidar_slow_dist_m'] == 1.00
    assert st['lidar_slow_v_mps'] == 0.20
    assert st['lidar_stale_ms'] == 200
    assert st['lidar_sector_deg'] == 30


def test_old_file_without_lidar_keys():
    """An old control.json written before the LiDAR work still reads."""
    p = _tmp('lidar_old')
    json.dump({'enabled': True, 'mode': 'auto'}, open(p, 'w'))
    st = cs.read_state(p)
    assert st['enabled'] is True
    assert st['lidar_stop_dist_m'] == 0.50


# ── clamps ──────────────────────────────────────────────────────────

def test_stop_distance_clamps():
    assert cs.clamp_lidar_stop_dist_m(0.0) == cs.LIDAR_STOP_MIN_M
    assert cs.clamp_lidar_stop_dist_m(99.0) == cs.LIDAR_STOP_MAX_M
    assert cs.clamp_lidar_stop_dist_m(0.42) == 0.42
    # negatives and junk must not slip through
    assert cs.clamp_lidar_stop_dist_m(-5) == cs.LIDAR_STOP_MIN_M


def test_slow_distance_clamps():
    assert cs.clamp_lidar_slow_dist_m(0.0) == cs.LIDAR_SLOW_MIN_M
    assert cs.clamp_lidar_slow_dist_m(99.0) == cs.LIDAR_SLOW_MAX_M
    assert cs.clamp_lidar_slow_dist_m(0.75) == 0.75


def test_slow_speed_clamps():
    assert cs.clamp_lidar_slow_v_mps(0.0) == cs.LIDAR_SLOW_V_MIN
    assert cs.clamp_lidar_slow_v_mps(2.0) == cs.LIDAR_SLOW_V_MAX
    assert cs.clamp_lidar_slow_v_mps(0.25) == 0.25


def test_stale_ms_clamps():
    assert cs.clamp_lidar_stale_ms(0) == cs.LIDAR_STALE_MIN_MS
    assert cs.clamp_lidar_stale_ms(60000) == cs.LIDAR_STALE_MAX_MS
    assert cs.clamp_lidar_stale_ms(350) == 350


def test_sector_deg_snaps_to_divisor():
    """sector_deg must divide 360 evenly or the last sector is a sliver."""
    for v, expected in ((15, 15), (20, 20), (30, 30), (45, 45), (60, 60), (90, 90)):
        assert cs.clamp_lidar_sector_deg(v) == expected
    # off-grid values snap to the nearest choice, never to a sliver
    assert cs.clamp_lidar_sector_deg(17) == 15
    assert cs.clamp_lidar_sector_deg(37) == 30
    assert cs.clamp_lidar_sector_deg(1000) == 90
    assert cs.clamp_lidar_sector_deg(1) == 15
    for deg in (15, 20, 30, 45, 60, 90):
        assert 360 % deg == 0


# ── write / read round trip ─────────────────────────────────────────

def test_write_then_read_round_trip():
    p = _tmp('lidar_rt')
    st = cs.write_state(lidar={'lidar_stop_dist_m': 0.65,
                               'lidar_slow_dist_m': 1.4,
                               'lidar_slow_v_mps': 0.15,
                               'lidar_stale_ms': 400,
                               'lidar_sector_deg': 45}, path=p)
    assert st['lidar_stop_dist_m'] == 0.65
    back = cs.read_state(p)
    for key in ('lidar_stop_dist_m', 'lidar_slow_dist_m', 'lidar_slow_v_mps',
                'lidar_stale_ms', 'lidar_sector_deg'):
        assert back[key] == st[key], (key, back[key], st[key])


def test_write_clamps_out_of_range():
    p = _tmp('lidar_clamp')
    st = cs.write_state(lidar={'lidar_stop_dist_m': 5.0,
                               'lidar_slow_v_mps': 9.0,
                               'lidar_stale_ms': 99999,
                               'lidar_sector_deg': 89}, path=p)
    assert st['lidar_stop_dist_m'] == cs.LIDAR_STOP_MAX_M
    assert st['lidar_slow_v_mps'] == cs.LIDAR_SLOW_V_MAX
    assert st['lidar_stale_ms'] == cs.LIDAR_STALE_MAX_MS
    assert st['lidar_sector_deg'] == 90
    # and it is what actually landed on disk
    assert cs.read_state(p)['lidar_stop_dist_m'] == cs.LIDAR_STOP_MAX_M


def test_read_clamps_hand_edited_file():
    """A hand-edited control.json cannot widen the zones."""
    p = _tmp('lidar_handedit')
    json.dump({'lidar_stop_dist_m': 99.0, 'lidar_stale_ms': 60000,
               'lidar_sector_deg': 7, 'lidar_slow_v_mps': -3.0}, open(p, 'w'))
    st = cs.read_state(p)
    assert st['lidar_stop_dist_m'] == cs.LIDAR_STOP_MAX_M
    assert st['lidar_stale_ms'] == cs.LIDAR_STALE_MAX_MS
    assert st['lidar_sector_deg'] == 15
    assert st['lidar_slow_v_mps'] == cs.LIDAR_SLOW_V_MIN


# ── invariants ──────────────────────────────────────────────────────

def test_slow_zone_always_clears_stop_zone():
    """Braking to a crawl inside the stop distance would be fatal: the
    rover must slow *before* the stop threshold, always."""
    p = _tmp('lidar_gap')
    # slow sent below stop -> pushed up to stop + gap
    st = cs.write_state(lidar={'lidar_stop_dist_m': 0.90,
                               'lidar_slow_dist_m': 0.40}, path=p)
    assert st['lidar_slow_dist_m'] >= st['lidar_stop_dist_m'] + cs.LIDAR_SLOW_GAP_M
    # raising only the stop distance pushes the slow zone with it
    st = cs.write_state(lidar={'lidar_stop_dist_m': 1.20}, path=p)
    assert st['lidar_slow_dist_m'] >= st['lidar_stop_dist_m'] + cs.LIDAR_SLOW_GAP_M
    # and the same holds when read back off disk
    assert cs.read_state(p)['lidar_slow_dist_m'] >= \
        cs.read_state(p)['lidar_stop_dist_m'] + cs.LIDAR_SLOW_GAP_M


def test_gap_never_exceeds_slow_range():
    """Whichever stop distance is set, the pushed-up slow distance stays
    inside its own clamp range — the gap must never un-clamp the value."""
    p = _tmp('lidar_gap_max')
    for stop in (0.20, 0.50, 1.00, cs.LIDAR_STOP_MAX_M):
        st = cs.write_state(lidar={'lidar_stop_dist_m': stop,
                                   'lidar_slow_dist_m': 0.3}, path=p)
        slow = st['lidar_slow_dist_m']
        assert cs.LIDAR_SLOW_MIN_M <= slow <= cs.LIDAR_SLOW_MAX_M, (stop, slow)
        assert slow >= st['lidar_stop_dist_m'] + cs.LIDAR_SLOW_GAP_M, (stop, slow)


def test_unknown_lidar_key_is_rejected():
    p = _tmp('lidar_unknown')
    try:
        cs.write_state(lidar={'lidar_not_a_setting': 1}, path=p)
    except ValueError:
        pass
    else:
        raise AssertionError('unknown lidar key should not be silently ignored')


def test_other_writes_preserve_lidar_settings():
    """Unrelated control writes (stop, mode, record) must leave the
    LiDAR zones alone — the AGV writes this file constantly."""
    p = _tmp('lidar_preserve')
    cs.write_state(lidar={'lidar_stop_dist_m': 0.7, 'lidar_sector_deg': 60}, path=p)
    cs.write_state(enabled=True, path=p)
    cs.write_state(mode='manual', manual=(0.5, 0.0), path=p)
    cs.write_state(record=True, path=p)
    st = cs.read_state(p)
    assert st['lidar_stop_dist_m'] == 0.7
    assert st['lidar_sector_deg'] == 60


def test_lidar_enabled_toggle():
    p = _tmp('lidar_enable')
    assert cs.write_state(lidar={'lidar_enabled': False}, path=p)['lidar_enabled'] is False
    assert cs.read_state(p)['lidar_enabled'] is False
    assert cs.write_state(lidar={'lidar_enabled': True}, path=p)['lidar_enabled'] is True


if __name__ == '__main__':
    _env.run_tests(globals())