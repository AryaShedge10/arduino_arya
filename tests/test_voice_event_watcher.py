"""Tests for voice.event_watcher — reads control.json + points.json,
fires persona events.

These tests write JSON files directly into a temp dir (instead of
running the full AGV) so we can deterministically trigger transitions
and assert what Tarka says in response.
"""

import _env  # noqa: F401

import json
import tempfile
import time
from pathlib import Path

from voice.event_watcher import EventWatcher
from voice.tts import SilentTts


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _make_watcher(directory):
    speaker = SilentTts()
    watcher = EventWatcher(
        telemetry_dir=directory,
        speaker=speaker,
        poll_interval_s=0.01,
        # Speed up debounce for the points test
        hold_debounce_s=0.05,
    )
    return watcher, speaker


def _base_control(**overrides):
    base = {'enabled': False, 'mode': 'auto', 'obstacle_stop': True,
            'lidar_enabled': True}
    base.update(overrides)
    return base


# ── control.json transitions ────────────────────────────────────────

def test_first_tick_does_not_fire():
    """Initial state must be silent — we don't want Tarka introducing
    herself every time the watcher restarts."""
    with tempfile.TemporaryDirectory() as d:
        _write_json(Path(d, 'control.json'), _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
    assert speaker.spoken == []


def test_motors_enabled_fires_event_with_mode_slot():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(enabled=True))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'motors_enabled' in events
    utter = next(s.utterance for s in speaker.spoken if s.event == 'motors_enabled')
    assert 'auto' in utter


def test_motors_enabled_announces_manual_mode():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(enabled=True, mode='manual'))
        watcher.tick()
    utter = next(s.utterance for s in speaker.spoken if s.event == 'motors_enabled')
    assert 'manual' in utter


def test_motors_disabled_fires_event():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control(enabled=True))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(enabled=False))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'motors_disabled' in events


def test_mode_changed_fires_only_while_enabled():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control(enabled=True))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(enabled=True, mode='manual'))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'mode_changed' in events
    utter = next(s.utterance for s in speaker.spoken if s.event == 'mode_changed')
    assert utter == 'manual mode.'


def test_mode_change_while_disabled_is_silent():
    """Don't narrate mode changes the operator can't see — that would
    be confusing when the rover is off."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(mode='manual'))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'mode_changed' not in events


def test_obstacle_stop_password_override_announces():
    """The dashboard password-gates obstacle_stop=False; when that fires,
    Tarka announces it so anyone in earshot hears that a safety was
    just disabled."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(obstacle_stop=False))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'obstacle_stop_disabled' in events
    utter = next(s.utterance
                 for s in speaker.spoken
                 if s.event == 'obstacle_stop_disabled')
    assert 'Test bench mode active' in utter


def test_lidar_password_override_announces():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control())
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(lidar_enabled=False))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'lidar_disabled' in events


def test_obstacle_stop_rearmed_does_not_announce():
    """Going the *other* way (off → on) doesn't need an announcement —
    nothing dangerous just happened."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        _write_json(ctl, _base_control(obstacle_stop=False))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(ctl, _base_control(obstacle_stop=True))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'obstacle_stop_disabled' not in events


# ── points.json transitions ─────────────────────────────────────────

def _points_payload(stop_count=0):
    points = [{
        't': i * 0.1,
        'a': float(i),
        'd': 0.3,         # < 0.50 default stop_dist → 'stop' zone
        'zone': 'stop',
    } for i in range(stop_count)]
    return {
        'updated_at': time.time(),
        'window_s': 2.0,
        'lidar_stop_dist_m': 0.50,
        'lidar_slow_dist_m': 1.00,
        'points': points,
    }


def test_hold_does_not_fire_before_debounce_expires():
    """First tick that sees an obstacle only arms the debounce; the
    phrase fires only after HOLD_DEBOUNCE_S elapses."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, _points_payload(stop_count=0))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(pts, _points_payload(stop_count=2))
        # Several short polls inside the debounce window — none of them
        # should fire.
        for _ in range(3):
            watcher.tick()
    assert 'lidar_hold' not in [s.event for s in speaker.spoken]


def test_hold_fires_once_after_debounce():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, _points_payload(stop_count=0))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(pts, _points_payload(stop_count=2))
        # First post-write tick arms the debounce timer; the second
        # tick, after the debounce window, actually fires.
        watcher.tick()
        time.sleep(0.10)
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'lidar_hold' in events
    utter = next(s.utterance for s in speaker.spoken if s.event == 'lidar_hold')
    assert 'Holding' in utter
    assert 'obstacle ahead' in utter


def test_hold_does_not_fire_repeatedly_while_obstacle_persists():
    """Once lidar_hold has fired, subsequent ticks with the obstacle
    still present must not re-fire. The persona is queue-and-hold, not
    spam."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, _points_payload(stop_count=0))
        watcher, speaker = _make_watcher(d)
        watcher.tick()
        _write_json(pts, _points_payload(stop_count=2))
        # First armed-tick; then sleep past debounce; then fire.
        watcher.tick()
        time.sleep(0.10)
        watcher.tick()           # fires once
        # Many more ticks, obstacle still there — must NOT re-fire.
        for _ in range(20):
            _write_json(pts, _points_payload(stop_count=2))
            watcher.tick()
    hold_count = sum(1 for s in speaker.spoken if s.event == 'lidar_hold')
    assert hold_count == 1


def test_lidar_clear_fires_when_stop_zone_drains():
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, _points_payload(stop_count=2))
        watcher, speaker = _make_watcher(d)
        # Tick once to establish the 'with-obstacle' baseline state.
        watcher.tick()
        # Simulate a hold that has already fired.
        watcher._hold.active = True
        # Now the stop zone empties.
        _write_json(pts, _points_payload(stop_count=0))
        watcher.tick()
    events = [s.event for s in speaker.spoken]
    assert 'lidar_clear' in events


def test_hold_debounce_resets_when_obstacle_disappears_early():
    """An obstacle that flutters in and out within the debounce window
    must not fire. The timer resets every time the zone goes back to 0."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, _points_payload(stop_count=0))
        watcher, speaker = _make_watcher(d)
        watcher.tick()

        # Obstacle appears, but only briefly each time
        for _ in range(5):
            _write_json(pts, _points_payload(stop_count=2))
            watcher.tick()
            _write_json(pts, _points_payload(stop_count=0))
            watcher.tick()
            time.sleep(0.02)
    assert 'lidar_hold' not in [s.event for s in speaker.spoken]


# ── Failure mode resilience ─────────────────────────────────────────

def test_missing_files_are_ignored_not_crashed():
    """Watcher running before the AGV has written anything must not blow
    up."""
    with tempfile.TemporaryDirectory() as d:
        watcher, speaker = _make_watcher(d)
        watcher.tick()
    assert speaker.spoken == []


def test_corrupt_json_does_not_kill_watcher():
    """The main project writes JSON atomically (tmp + rename). We should
    never see a partial file, but defensive code lives here so a bad
    write doesn't take down narration."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'control.json').write_text('not json at all {')
        Path(d, 'points.json').write_text('also not json')
        watcher, _ = _make_watcher(d)
        watcher.tick()
        watcher.tick()


def test_partial_control_does_not_crash():
    """A control.json missing some keys (older schema, hand edit) is
    still read safely."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'control.json').write_text(json.dumps({'enabled': True}))
        watcher, speaker = _make_watcher(d)
        watcher.tick()         # baseline
        watcher.tick()         # second tick — no change, no fire
    assert speaker.spoken == []


def test_points_with_missing_zone_key_is_handled():
    """A points.json from an older publisher might not tag each point
    with a zone. The watcher must not crash on those rows."""
    with tempfile.TemporaryDirectory() as d:
        ctl = Path(d, 'control.json')
        pts = Path(d, 'points.json')
        _write_json(ctl, _base_control())
        _write_json(pts, {'points': [{'t': 0, 'a': 0, 'd': 0.3}],      # no 'zone'
                        'lidar_stop_dist_m': 0.5})
        watcher, _ = _make_watcher(d)
        watcher.tick()
        # Behaviour is "no stop-zone points seen" -> silent; the test
        # only proves we didn't crash.


if __name__ == '__main__':
    _env.run_tests(globals())
