"""rec_row: legacy keys keep their exact values; new keys are present."""
import _env  # noqa: F401  (must be first)
import json
import math
from types import SimpleNamespace as NS

import rec_row
from config import TICKS_PER_M, WHEEL_DIAMETER_M


def _args():
    line = NS(found=True, offset_m=0.123456, angle_deg=-3.14159, lookahead_m=0.45678,
              look_dist_m=0.98765, curvature=0.012345, strips_used=5, reason='ok')
    obst = NS(state='CLEAR', raw_state='SLOW', distance_m=1.23456)
    d = NS(direction=NS(value='LEFT'), final_cmd='L', track='OK', boost=1.2345,
           emergency=False, status_text='go')
    wheels = NS(left=40, right=-41, left_mps=0.2, right_mps=0.25, closed=True)
    snap = NS(applied_left=38, applied_right=-39, meas_ticks_left=int(TICKS_PER_M * 0.5),
              meas_ticks_right=100, encoder_left=1500, encoder_right=-700,
              enc_zero_left=500, enc_zero_right=-200, fault_bits=2,
              last_ack_monotonic=99.9, fw_mode='CL', pwm_target_left=41,
              pwm_target_right=-42, firmware_version='v2.1')
    return dict(seq=7, frame_mono=99.8, t_mono=100.0, ctl={'mode': 'AUTO'}, enabled=True,
                speed_pct=60, line=line, obst=obst, enforce=True, d=d, v_t=0.2234, w_t=-1.26,
                wheels=wheels, snap=snap, link_ok=True, vision_ms=12.345,
                curve_px=[[1, 2]], look_px=[3, 4], closed_wanted=True, fw_ready=False,
                req_mono=99.85)


def test_legacy_values():
    row = rec_row.build_log_row(**_args())
    meas_l = int(TICKS_PER_M * 0.5) / TICKS_PER_M
    assert row['seq'] == 7 and row['mode'] == 'AUTO' and row['enabled'] is True
    assert row['speed_pct'] == 60 and row['link_ok'] is True and row['vision_ms'] == 12.3
    assert row['line'] == {'found': True, 'offset_m': 0.1235, 'angle_deg': -3.14,
                           'lookahead_m': 0.4568, 'look_dist_m': 0.988, 'curvature': 0.0123,
                           'strips': 5, 'reason': 'ok', 'curve_px': [[1, 2]], 'look_px': [3, 4]}
    assert row['obstacle']['state'] == 'CLEAR' and row['obstacle']['raw'] == 'SLOW'
    assert row['obstacle']['distance_m'] == 1.235
    assert row['decision'] == {'direction': 'LEFT', 'cmd': 'L', 'track': 'OK', 'boost': 1.23,
                               'emergency': False, 'text': 'go', 'v': 0.2, 'omega': -1.3}
    w = row['wheels']
    assert (w['pwm_l'], w['pwm_r'], w['applied_l'], w['applied_r']) == (40, -41, 38, -39)
    assert (w['tgt_l_mps'], w['tgt_r_mps']) == (0.2, 0.25)
    assert w['meas_l_mps'] == round(meas_l, 3) and w['meas_r_mps'] == round(100 / TICKS_PER_M, 3)
    assert w['closed_loop'] is True


def test_new_fields():
    row = rec_row.build_log_row(**_args())
    assert row['t_mono'] == 100.0 and row['frame_mono'] == 99.8
    assert row['frame_age_ms'] == 200.0
    assert row['closed_loop_wanted'] is True and row['fw_closed_ready'] is False
    assert row['obstacle']['enforced'] is True
    w = row['wheels']
    assert w['rpm_l'] == round(int(TICKS_PER_M * 0.5) / TICKS_PER_M / (math.pi * WHEEL_DIAMETER_M) * 60, 1)
    assert abs(w['rpm_l'] - 0.5 / (math.pi * WHEEL_DIAMETER_M) * 60) < 1.5
    assert w['tgt_tps_l'] == round(0.2 * TICKS_PER_M, 1)
    assert w['meas_tps_r'] == 100 and w['pwm_target_l'] == 41 and w['pwm_target_r'] == -42
    assert w['fw_mode'] == 'CL'
    e = row['enc']
    assert e == {'ticks_l': 1500, 'ticks_r': -700, 'rel_l': 1000, 'rel_r': -500,
                 'fault_bits': 2, 'fault_l': False, 'fault_r': True,
                 'ack_mono': 99.9, 'ack_age_ms': 100.0, 'fw': 'v2.1'}
    json.dumps(row)


def test_missing_attrs_and_no_frame_mono():
    a = _args()
    a['snap'] = NS()
    a['frame_mono'] = None
    row = rec_row.build_log_row(**a)
    assert row['frame_age_ms'] is None and row['enc']['ack_age_ms'] is None
    json.dumps(row)


def test_telem_row():
    row = rec_row.build_log_row(**_args())
    t = rec_row.build_telem_row(row)
    assert list(t) == ['t', 't_mono', 'seq', 'enabled', 'mode', 'found', 'offset_m', 'angle_deg',
                       'obst_raw', 'v', 'omega', 'ticks_l', 'ticks_r', 'ack_mono', 'meas_l_mps',
                       'meas_r_mps', 'tgt_l_mps', 'tgt_r_mps', 'applied_l', 'applied_r',
                       'fault_bits', 'errors', 'latency_ms']
    assert t['t'] is None and t['obst_raw'] == 'SLOW' and t['ticks_l'] == 1500
    # fault_l = False (snap.fault_bits=2 → bit 1 unset), fault_r = True → errors only when at least one
    assert t['errors'] == ['enc_fault_r']
    # latency_ms = (ack_mono - req_mono) * 1000 = (99.9 - 99.85) * 1000 = 50.0
    assert t['latency_ms'] == round((99.9 - 99.85) * 1000.0, 2)
    json.dumps(t)


def test_telem_row_no_errors_no_latency():
    """When fault_bits=0 and req_mono is missing, the optional keys are omitted."""
    a = _args()
    a['snap'] = NS(applied_left=0, applied_right=0, meas_ticks_left=0, meas_ticks_right=0,
                    encoder_left=0, encoder_right=0, enc_zero_left=0, enc_zero_right=0,
                    fault_bits=0, last_ack_monotonic=None, fw_mode='P',
                    pwm_target_left=0, pwm_target_right=0, firmware_version='v2')
    row = rec_row.build_log_row(**a)
    t = rec_row.build_telem_row(row)
    assert 'errors' not in t
    assert 'latency_ms' not in t
    json.dumps(t)


def test_decode_faults():
    assert rec_row.decode_faults(0) == []
    assert rec_row.decode_faults(1) == ['enc_fault_l']
    assert rec_row.decode_faults(2) == ['enc_fault_r']
    assert rec_row.decode_faults(3) == ['enc_fault_l', 'enc_fault_r']
    assert rec_row.decode_faults(7) == ['enc_fault_l', 'enc_fault_r', 'enc_fault_warn']
    assert rec_row.decode_faults(None) == []


if __name__ == '__main__':
    _env.run_tests(globals())
