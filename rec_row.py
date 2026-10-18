"""Pure builders for the recorder's per-frame log row and telemetry row."""
from __future__ import annotations

import math

from config import TICKS_PER_M, WHEEL_DIAMETER_M

_RPM_PER_MPS = 60.0 / (math.pi * WHEEL_DIAMETER_M)


def _r(v, n):
    return None if v is None else round(v, n)


def build_log_row(*, seq, frame_mono, t_mono, ctl, enabled, speed_pct, line, obst,
                  enforce, d, v_t, w_t, wheels, snap, link_ok, vision_ms,
                  curve_px, look_px, closed_wanted, fw_ready) -> dict:
    g = lambda name, default=0: getattr(snap, name, default)  # noqa: E731
    meas_l = g('meas_ticks_left') / TICKS_PER_M
    meas_r = g('meas_ticks_right') / TICKS_PER_M
    enc_l, enc_r = g('encoder_left'), g('encoder_right')
    faults = g('fault_bits')
    ack = g('last_ack_monotonic', None)
    ack_age = None
    if ack is not None and t_mono is not None:
        ack_age = round((t_mono - ack) * 1000.0, 1)
    frame_age = None
    if frame_mono is not None and t_mono is not None:
        frame_age = round((t_mono - frame_mono) * 1000.0, 1)
    return {
        'seq': seq, 'mode': ctl['mode'], 'enabled': enabled,
        'speed_pct': speed_pct,
        't_mono': t_mono, 'frame_mono': frame_mono, 'frame_age_ms': frame_age,
        'closed_loop_wanted': closed_wanted, 'fw_closed_ready': fw_ready,
        'line': {'found': line.found, 'offset_m': round(line.offset_m, 4),
                 'angle_deg': round(line.angle_deg, 2), 'lookahead_m': round(line.lookahead_m, 4),
                 'look_dist_m': round(line.look_dist_m, 3), 'curvature': round(line.curvature, 4),
                 'strips': line.strips_used, 'reason': line.reason,
                 'curve_px': curve_px, 'look_px': look_px},
        'obstacle': {'state': obst.state, 'raw': obst.raw_state,
                     'distance_m': round(obst.distance_m, 3), 'enforced': enforce},
        'decision': {'direction': d.direction.value, 'cmd': d.final_cmd, 'track': d.track,
                     'boost': round(d.boost, 2), 'emergency': d.emergency, 'text': d.status_text,
                     'v': round(v_t, 1), 'omega': round(w_t, 1)},
        'wheels': {'pwm_l': wheels.left, 'pwm_r': wheels.right,
                   'applied_l': g('applied_left'), 'applied_r': g('applied_right'),
                   'tgt_l_mps': round(wheels.left_mps, 3), 'tgt_r_mps': round(wheels.right_mps, 3),
                   'meas_l_mps': round(meas_l, 3), 'meas_r_mps': round(meas_r, 3),
                   'closed_loop': wheels.closed,
                   'rpm_l': round(meas_l * _RPM_PER_MPS, 1), 'rpm_r': round(meas_r * _RPM_PER_MPS, 1),
                   'meas_tps_l': g('meas_ticks_left'), 'meas_tps_r': g('meas_ticks_right'),
                   'tgt_tps_l': round(wheels.left_mps * TICKS_PER_M, 1),
                   'tgt_tps_r': round(wheels.right_mps * TICKS_PER_M, 1),
                   'pwm_target_l': g('pwm_target_left'), 'pwm_target_r': g('pwm_target_right'),
                   'fw_mode': g('fw_mode', None)},
        'enc': {'ticks_l': enc_l, 'ticks_r': enc_r,
                'rel_l': enc_l - g('enc_zero_left'), 'rel_r': enc_r - g('enc_zero_right'),
                'fault_bits': faults, 'fault_l': bool(faults & 1), 'fault_r': bool(faults & 2),
                'ack_mono': ack, 'ack_age_ms': ack_age, 'fw': g('firmware_version', None)},
        'link_ok': link_ok, 'vision_ms': round(vision_ms, 1),
    }


def build_telem_row(row: dict) -> dict:
    line, obst = row.get('line', {}), row.get('obstacle', {})
    dec, w, enc = row.get('decision', {}), row.get('wheels', {}), row.get('enc', {})
    return {
        't': row.get('t'), 't_mono': row.get('t_mono'), 'seq': row.get('seq'),
        'enabled': row.get('enabled'), 'mode': row.get('mode'),
        'found': line.get('found'), 'offset_m': line.get('offset_m'),
        'angle_deg': line.get('angle_deg'), 'obst_raw': obst.get('raw'),
        'v': dec.get('v'), 'omega': dec.get('omega'),
        'ticks_l': enc.get('ticks_l'), 'ticks_r': enc.get('ticks_r'),
        'ack_mono': enc.get('ack_mono'),
        'meas_l_mps': w.get('meas_l_mps'), 'meas_r_mps': w.get('meas_r_mps'),
        'tgt_l_mps': w.get('tgt_l_mps'), 'tgt_r_mps': w.get('tgt_r_mps'),
        'applied_l': w.get('applied_l'), 'applied_r': w.get('applied_r'),
        'fault_bits': enc.get('fault_bits'),
    }
