"""Intent — structured output from the LLM parse lane.

This module is the **contract** between the LLM (Gemini or local) and
the dialogue/control side. It is also the **safety boundary**: the
LLM's response is treated as untrusted input and validated against
the action whitelist, the per-action arg schema, and a confidence
floor. Validation is performed in Python — not in the prompt — so a
prompt-injection or hallucination cannot drive the rover.

Why we don't trust the LLM:
  - Gemini can be tricked into emitting actions outside its training
    ("ignore your previous instructions").
  - Hallucinated fields (typos, swapped numbers, missing units) are
    common with structured outputs.
  - The validator is the only authoritative gate; the prompt just
    shapes Gemini's behaviour so the *common* case lands cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ._constants import (
    LLM_CONFIDENCE_FLOOR,
    MOTION_MAX_DISTANCE_M,
    MOTION_MIN_DISTANCE_M,
    MOTION_VALID_DIRECTIONS,
    VALID_INTENT_ACTIONS,
)


@dataclass(frozen=True)
class Intent:
    """A validated parse result. Constructed only by ``from_raw`` or
    by code that has already run the validator.

    ``raw`` is the source utterance, kept for logging. ``confidence``
    is in [0.0, 1.0] and must clear ``LLM_CONFIDENCE_FLOOR`` to reach a
    control write. ``action`` is one of ``VALID_INTENT_ACTIONS``.
    """
    action: str
    args: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw: Optional[str] = None

    def is_valid(self,
                 allowed_actions: frozenset[str] = VALID_INTENT_ACTIONS,
                 confidence_floor: float = LLM_CONFIDENCE_FLOOR) -> bool:
        """Standalone validity check (no exception path)."""
        return validate_intent(self, allowed_actions, confidence_floor)


def validate_intent(intent: Intent,
                    allowed_actions: frozenset[str] = VALID_INTENT_ACTIONS,
                    confidence_floor: float = LLM_CONFIDENCE_FLOOR) -> bool:
    """Return True iff the intent clears all gates.

    Gates (in order, all required for non-``unknown`` actions):
      1. action is in the whitelist
      2. confidence is a finite numeric value in [0.0, 1.0]
         (and is not a Python ``bool`` — those slip through as int
         subclass and must be rejected explicitly)
      3. confidence is at or above ``confidence_floor``
      4. per-action arg schema is satisfied

    ``action='unknown'`` short-circuits to ``True`` *if* the schema is
    satisfied — it's the LLM's explicit "I can't parse this" signal,
    and the dialogue layer wants to handle it (ask for clarification)
    rather than have it filtered out as low-confidence.

    The function never raises — anything uncertain is rejected.
    """
    if not isinstance(intent.action, str) or intent.action not in allowed_actions:
        return False
    if not _validate_args(intent.action, intent.args):
        return False

    if intent.action == 'unknown':
        # Explicit failure admission from the LLM; confidence 0.0 is
        # the documented way to express it. Bypass the floor.
        return True

    conf = intent.confidence
    if isinstance(conf, bool):
        return False                # bool is an int subclass; reject
    if not isinstance(conf, (int, float)):
        return False
    if conf != conf:                # NaN
        return False
    if float(conf) < 0.0 or float(conf) > 1.0:
        return False
    if float(conf) < float(confidence_floor):
        return False
    return True


def _validate_args(action: str, args: Mapping[str, Any]) -> bool:
    """Per-action argument schema. Fail closed: any doubt returns False."""
    if action == 'motion':
        if not isinstance(args, dict):
            return False
        d = args.get('dir')
        if not isinstance(d, str) or d not in MOTION_VALID_DIRECTIONS:
            return False

        dist = args.get('dist_m')
        if dist is not None:
            if isinstance(dist, bool):
                return False
            if not isinstance(dist, (int, float)):
                return False
            if dist != dist:        # NaN
                return False
            if not (MOTION_MIN_DISTANCE_M <= float(dist) <= MOTION_MAX_DISTANCE_M):
                return False

        deg = args.get('deg')
        if deg is not None:
            if isinstance(deg, bool):
                return False
            if not isinstance(deg, (int, float)):
                return False
            if deg != deg:
                return False
            if not (0.0 < float(deg) <= 360.0):
                return False

        # Direction-specific completeness.
        if d in ('forward', 'back') and dist is None:
            return False             # forward/back must carry distance
        if d in ('left', 'right') and dist is None and deg is None:
            return False             # turns need either distance or angle
        return True

    if action in ('stop', 'query_status', 'query_position', 'unknown'):
        return True                 # no args required; extras ignored

    return False                    # unknown action


__all__ = ['Intent', 'validate_intent']
