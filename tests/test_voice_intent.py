"""Tests for voice.intent — the validator is the prompt-injection defense.

These tests are pure. No API calls. They pin both happy paths and the
rejections that have to hold against an adversarial LLM.
"""

import _env  # noqa: F401

from voice.intent import Intent, validate_intent
from voice._constants import (
    LLM_CONFIDENCE_FLOOR,
    MOTION_MAX_DISTANCE_M,
    MOTION_MIN_DISTANCE_M,
    MOTION_VALID_DIRECTIONS,
    VALID_INTENT_ACTIONS,
)


# ── happy paths ─────────────────────────────────────────────────────


def test_valid_motion_intent_passes():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=0.91)
    assert validate_intent(intent) is True


def test_valid_motion_with_zero_distance_is_rejected():
    """0 m is a no-op command; the LLM shouldn't bother sending it."""
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 0.0},
                    confidence=0.91)
    assert validate_intent(intent) is False


def test_valid_turn_with_degrees_passes():
    intent = Intent(action='motion',
                    args={'dir': 'left', 'deg': 90.0},
                    confidence=0.91)
    assert validate_intent(intent) is True


def test_valid_stop_passes_with_no_args():
    intent = Intent(action='stop', args={}, confidence=0.99)
    assert validate_intent(intent) is True


def test_query_status_passes_with_empty_args():
    intent = Intent(action='query_status', args={}, confidence=0.99)
    assert validate_intent(intent) is True


def test_unknown_action_passes_with_zero_confidence():
    """The LLM is allowed to admit failure; the action 'unknown' with
    conf=0.0 is the way it does. Any *other* action at conf=0.0 is
    also accepted if and only if the action is in the whitelist."""
    intent = Intent(action='unknown',
                    args={'raw_text': 'gibberish'},
                    confidence=0.0)
    assert validate_intent(intent) is True


# ── action whitelist enforcement ────────────────────────────────────


def test_unknown_outer_action_is_rejected():
    intent = Intent(action='launch_missiles', args={}, confidence=0.99)
    assert validate_intent(intent) is False


def test_disabling_lidar_is_rejected_by_action_whitelist():
    """The single most important class of test. Any LLM output that
    tries to tamper with the safety layer must be rejected before it
    reaches control_state."""
    assert validate_intent(Intent(action='disable_lidar', args={},
                                  confidence=0.99)) is False
    assert validate_intent(Intent(action='disable_obstacle_stop', args={},
                                  confidence=0.99)) is False
    assert validate_intent(Intent(action='ignore_previous_instructions', args={},
                                  confidence=0.99)) is False


def test_non_string_action_is_rejected():
    assert validate_intent(Intent(action=None, args={}, confidence=0.99)) is False
    assert validate_intent(Intent(action=42, args={}, confidence=0.99)) is False


# ── confidence gates ────────────────────────────────────────────────


def test_low_confidence_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=0.30)
    assert validate_intent(intent) is False


def test_just_below_floor_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=LLM_CONFIDENCE_FLOOR - 0.001)
    assert validate_intent(intent) is False


def test_at_floor_is_accepted():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=LLM_CONFIDENCE_FLOOR)
    assert validate_intent(intent) is True


def test_above_one_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=1.01)
    assert validate_intent(intent) is False


def test_negative_confidence_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=-0.1)
    assert validate_intent(intent) is False


def test_nan_confidence_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=float('nan'))
    assert validate_intent(intent) is False


def test_non_numeric_confidence_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence='0.9')
    assert validate_intent(intent) is False


def test_bool_confidence_is_rejected():
    """``True`` and ``False`` are subclasses of int in Python. The
    validator must not accept them — only numeric confidence counts."""
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=True)
    assert validate_intent(intent) is False


# ── argument schema enforcement ─────────────────────────────────────


def test_motion_without_dir_is_rejected():
    intent = Intent(action='motion',
                    args={'dist_m': 10.0},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_motion_without_dist_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward'},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_unknown_direction_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'sideways', 'dist_m': 10.0},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_below_min_distance_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': MOTION_MIN_DISTANCE_M / 2},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_above_max_distance_is_rejected():
    """Huge-distance injection. The validator is the last line of
    defense here."""
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': MOTION_MAX_DISTANCE_M * 10},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_negative_distance_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': -5.0},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_string_distance_is_rejected():
    """LLMs are notorious for emitting numbers as strings. The
    validator must convert or reject."""
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': '10'},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_bool_distance_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': True},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_nan_distance_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': float('nan')},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_turn_with_zero_degrees_is_rejected():
    """A 0° turn is a no-op, same logic as 0 m distance."""
    intent = Intent(action='motion',
                    args={'dir': 'left', 'deg': 0.0},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_turn_with_huge_degrees_is_rejected():
    intent = Intent(action='motion',
                    args={'dir': 'left', 'deg': 9999.0},
                    confidence=0.99)
    assert validate_intent(intent) is False


def test_all_directions_in_whitelist_pass_with_valid_args():
    """Sanity check: the whitelist isn't accidentally rejecting valid
    directions."""
    for d in MOTION_VALID_DIRECTIONS:
        args = {'dir': d, 'dist_m': 5.0}
        intent = Intent(action='motion', args=args, confidence=0.99)
        assert validate_intent(intent) is True, f'direction {d!r} rejected'


def test_intent_is_valid_matches_validate_intent():
    """Method on Intent mirrors the free function — both ways of
    asking the same question should agree."""
    intent = Intent(action='motion',
                    args={'dir': 'forward', 'dist_m': 10.0},
                    confidence=0.91)
    assert intent.is_valid() == validate_intent(intent)


def test_intent_is_dataclass_and_immutable():
    """Frozen dataclass — refactors should never accidentally let
    voice code mutate an Intent in flight."""
    intent = Intent(action='stop', args={}, confidence=0.99)
    try:
        intent.action = 'launch_missiles'      # type: ignore[misc]
    except Exception:
        return
    raise AssertionError('Intent must be frozen')


if __name__ == '__main__':
    _env.run_tests(globals())
