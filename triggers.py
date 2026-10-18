"""Recorder triggers: pure functions of a log row (see PLAN_RECORDER_V1.md, E)."""
from __future__ import annotations

import config

TRIGGER_NAMES = (
    'line_lost', 'track_stuck', 'track_drifting', 'track_off_line',
    'obstacle_slow', 'obstacle_stop', 'implausible', 'jump', 'enc_fault', 'link_down',
)

JUMP_MAX_DT_S = 0.5


def legacy_problem(row) -> str:
    """The v0 `problem` string, reproduced from a log row."""
    line = row.get('line') or {}
    track = (row.get('decision') or {}).get('track')
    raw = (row.get('obstacle') or {}).get('raw')
    problem = ''
    if not line.get('found'):
        problem = 'line_lost'
    elif track in ('STUCK', 'DRIFTING', 'OFF_LINE'):
        problem = 'track_' + track.lower()
    if raw is not None and raw != 'CLEAR':
        problem = (problem + '+' if problem else '') + 'obstacle_' + str(raw).lower()
    return problem


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class TriggerEngine:
    def __init__(self, cooldown_s=None, max_offset_m=None, max_angle_deg=None,
                 min_strips=None, jump_m=None, jump_deg=None,
                 persist_s=None, rearm_s=None):
        c = config
        self.cooldown_s = c.REC_TRIG_COOLDOWN_S if cooldown_s is None else cooldown_s
        self.max_offset_m = c.REC_TRIG_MAX_OFFSET_M if max_offset_m is None else max_offset_m
        self.max_angle_deg = c.REC_TRIG_MAX_ANGLE_DEG if max_angle_deg is None else max_angle_deg
        self.min_strips = c.REC_TRIG_MIN_STRIPS if min_strips is None else min_strips
        self.jump_m = c.REC_TRIG_JUMP_M if jump_m is None else jump_m
        self.jump_deg = c.REC_TRIG_JUMP_DEG if jump_deg is None else jump_deg
        self.persist_s = c.REC_TRIG_PERSIST_S if persist_s is None else persist_s
        self.rearm_s = c.REC_TRIG_REARM_S if rearm_s is None else rearm_s
        self.reset()

    def reset(self) -> None:
        self._last_fire = {}
        self._last_true = {}   # name -> t_mono the condition was last seen true
        self._pending = {}     # name -> new episode began but cooldown held it back
        self._prev = None   # (t_mono, offset_m, angle_deg) of previous found row

    def _implausible(self, line) -> bool:
        off, look = _num(line.get('offset_m')), _num(line.get('lookahead_m'))
        ang, cur = _num(line.get('angle_deg')), _num(line.get('curvature'))
        strips = _num(line.get('strips'))
        return ((off is not None and abs(off) > self.max_offset_m)
                or (look is not None and abs(look) > self.max_offset_m)
                or (ang is not None and abs(ang) > self.max_angle_deg)
                or (strips is not None and strips < self.min_strips)
                or (cur is not None and abs(cur) > config.KAPPA_MAX))

    def evaluate(self, row, t_mono) -> list:
        row = row if isinstance(row, dict) else {}
        line = row.get('line') or {}
        raw = []
        found = bool(line.get('found'))
        if not found:
            raw.append('line_lost')
        else:
            if self._implausible(line):
                raw.append('implausible')
            off, ang = _num(line.get('offset_m')), _num(line.get('angle_deg'))
            if off is not None and ang is not None and t_mono is not None:
                if self._prev is not None:
                    pt, po, pa = self._prev
                    if 0 <= t_mono - pt < JUMP_MAX_DT_S and (
                            abs(off - po) > self.jump_m or abs(ang - pa) > self.jump_deg):
                        raw.append('jump')
                self._prev = (t_mono, off, ang)
            else:
                self._prev = None
        track = (row.get('decision') or {}).get('track')
        if found and track in ('STUCK', 'DRIFTING', 'OFF_LINE'):
            raw.append('track_' + track.lower())
        obs = (row.get('obstacle') or {}).get('raw')
        if obs in ('SLOW', 'STOP'):
            raw.append('obstacle_' + obs.lower())
        if (row.get('enc') or {}).get('fault_bits'):
            raw.append('enc_fault')
        if row.get('link_ok') is False:
            raw.append('link_down')
        if not found:
            self._prev = None
        if t_mono is None:
            return raw
        out = []
        for name in raw:
            lt = self._last_true.get(name)
            self._last_true[name] = t_mono
            if lt is None or not 0 <= t_mono - lt < self.rearm_s:
                self._pending[name] = True      # new episode
            last = self._last_fire.get(name)
            gap = None if last is None else t_mono - last
            if gap is not None and 0 <= gap < self.cooldown_s:
                continue
            if self._pending.get(name):
                pass
            elif gap is not None and 0 <= gap < self.persist_s:
                continue                         # still the same episode: suppressed
            self._pending[name] = False
            self._last_fire[name] = t_mono
            out.append(name)
        return out
