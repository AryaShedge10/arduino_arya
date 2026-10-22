"""Persona utterances — pure rendering from events + state.

Owns Tarka's voice. This module is intentionally side-effect free: it
turns an event name + slots into a string. The audio layer
(``voice.tts``) decides whether to play a recorded clip or synthesise.

No JSON, no audio, no I/O — fully testable in isolation. Any system
that wants Tarka to say something calls ``render_event``.
"""

from __future__ import annotations

from typing import Optional

from ._constants import (
    HOLD_PROMPT,
    PERSONA_EVENTS,
)


# Event → utterance template. Curated set; this is the *only* place
# Tarka's exact wording lives. The phrase cache in ``voice/phrases/`` is
# keyed by event name; keep event names stable so the cache stays valid.
_UTTERANCES: dict[str, str] = {
    # ── Canonical 10 (pinned by tests/test_voice_persona.py) ────────
    'wake_ready':          'Yes?',
    'motion_authorised':   'Moving.',
    'motion_complete':     'Done.',
    'emergency_stop':      'Stopped.',
    'mute_engaged':        'Voice off.',
    'mute_released':       'Voice on.',
    'confirm_timeout':     'No confirmation. Standing by.',
    'clarify_default':     "Sorry, I didn't catch that.",
    'out_of_scope':        "I can't do that from voice. Please use the dashboard.",
    # 'lidar_hold' uses the HOLD_PROMPT template — see below.

    # ── Hold family ────────────────────────────────────────────────
    'lidar_hold':          HOLD_PROMPT,        # 'Holding — {reason}. Will retry when clear.'
    'lidar_clear':         'Path clear.',

    # ── Slot-fill clarifications ───────────────────────────────────
    'clarify_direction':   'Sorry, which direction?',
    'clarify_distance':    'Distance?',

    # ── Status + fault announcements ───────────────────────────────
    'battery_low':         'Battery at {pct} percent. Returning to dock.',
    'stuck':               "I'm stuck. Need assistance.",

    # ── Phase 0 narration (read from the dashboard's control.json /
    #    points.json by voice.event_watcher) ────────────────────────
    'motors_enabled':      'Motors on. {mode} mode.',
    'motors_disabled':     'Motors off.',
    'mode_changed':        '{mode} mode.',
    'obstacle_stop_disabled':
        'Obstacle stop disabled. Password override accepted. Test bench mode active.',
    'lidar_disabled':
        'LiDAR disabled. Password override accepted. Test bench mode active.',

    # ── Motor / Drive state ────────────────────────────────────────
    'motors_enabled_manual': 'Motors on. Manual mode.',
    'speed_set':              'Speed set.',
    'speed_limit_reached':    'Already at maximum speed.',

    # ── Motion / Navigation feedback ──────────────────────────────
    'turning_left':    'Turning left.',
    'turning_right':   'Turning right.',
    'reversing':       'Reversing.',
    'pivot_aligning':  'Aligning.',
    'line_found':      'Line found.',
    'line_lost':       'Line lost. Searching.',
    'searching':       'Searching for the line.',
    'recovery_failed': "Can't find the line. Stopping.",

    # ── Obstacle nuance ────────────────────────────────────────────
    'obstacle_camera_slow': 'Camera obstacle. Slowing.',
    'obstacle_camera_stop': 'Camera obstacle. Stopped.',
    'obstacle_cleared':     'Obstacle cleared. Resuming.',
    'lidar_slow':           'Obstacle in slow zone.',

    # ── Encoder / Firmware faults ──────────────────────────────────
    'enc_fault_left':     'Left encoder fault. Falling back.',
    'enc_fault_right':    'Right encoder fault. Falling back.',
    'enc_fault_cleared':  'Encoder recovered.',
    'firmware_old':       'Old firmware detected. Please update.',
    'arduino_timeout':    'Arduino link timed out. Motors off.',
    'arduino_reconnected':'Arduino link restored.',

    # ── Camera / Vision ────────────────────────────────────────────
    'camera_stalled':   'Camera not responding. Motors off.',
    'camera_recovered': 'Camera is back.',
    'vision_degraded':  'Vision degraded. Reducing speed.',

    # ── Recording ──────────────────────────────────────────────────
    'recording_started': 'Recording.',
    'recording_stopped': 'Recording stopped.',
    'recording_marked':  'Frame marked.',
    'disk_almost_full':  'Disk almost full. Recording will stop soon.',

    # ── Battery / Power ────────────────────────────────────────────
    'battery_critical': 'Battery critical. Please charge immediately.',
    'battery_ok':       'Battery is fine.',
    'charging':         'Charging.',

    # ── Dialogue / Clarification extras ───────────────────────────
    'clarify_speed':              'What speed?',
    'clarify_confirm_or_cancel':  'Confirm or cancel?',
    'repeat_please':              'Could you repeat that?',
    'not_while_moving':           "Can't do that while moving.",
    'too_far':                    'That distance is out of range.',
    'too_close':                  'Distance too small to be safe.',
    'command_queued':             'Queued. Will go when clear.',
    'queue_cleared':              'No commands queued.',

    # ── Status query responses ─────────────────────────────────────
    'status_speed_pct':          'Speed is at {pct} percent.',
    'status_line_on':            'On the line.',
    'status_line_off':           'Not on the line.',
    'status_obstacle_distance':  'Nearest obstacle at {dist} {unit}.',
    'status_recording_on':       'Recording is active.',
    'status_recording_off':      'Not recording.',
    'status_ok':                 'All systems nominal.',

    # ── Safety overrides (password-gated) ─────────────────────────
    'obstacle_stop_enabled': 'Obstacle stop re-enabled.',
    'lidar_enabled_again':   'LiDAR safety restored.',
    'override_rejected':     'Override rejected. Wrong password.',
}


def _safe_format(template: str, slots: dict) -> str:
    """Replace ``{key}`` placeholders; leave unknown keys literal so the
    template survives even when a caller forgot a slot."""
    out = template
    for k, v in slots.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def render_event(event: str, **slots) -> Optional[str]:
    """Return Tarka's utterance for ``event``, or ``None`` if unknown.

    Unknown event names return ``None`` so callers can ignore them
    without importing ``PERSONA_EVENTS``. Missing slot values are
    rendered literally — keeps ``render_event`` safe to call from any
    watcher that may not have every slot.
    """
    if event not in PERSONA_EVENTS:
        return None
    template = _UTTERANCES.get(event)
    if template is None:
        return None
    return _safe_format(template, dict(slots))


def utterance_for_event(event: str, **slots) -> Optional[str]:
    """Alias for ``render_event`` — same behaviour, noun form for callers
    that prefer to read it as 'what does Tarka say for this event?'."""
    return render_event(event, **slots)


__all__ = ['render_event', 'utterance_for_event']
