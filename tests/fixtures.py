"""Shared test fixtures (no dependency on recorder.py)."""
import json
import os

import cv2
import numpy as np


def synthetic_frame(k: int, w: int = 1280, h: int = 720) -> np.ndarray:
    """Grey floor + a wobbling yellow line (like camera.FakeCamera)."""
    frame = np.full((h, w, 3), 80, dtype=np.uint8)
    cx = w // 2 + int(120 * np.sin(k * 0.3))
    cv2.line(frame, (cx, 0), (cx, h), (0, 255, 255), 12)
    return frame


def _row(i, n):
    found = i % 4 != 3
    row = {
        'i': i, 't': 1000.0 + i * 0.2, 'dt': round(i * 0.2, 3), 'file': f'frames/{i:06d}.jpg',
        'seq': i, 'mode': 'AUTO', 'enabled': True, 'speed_pct': 30, 'link_ok': True,
        'vision_ms': 5.0,
        'line': {'found': found, 'offset_m': 0.02 * (i % 3), 'angle_deg': 1.5,
                 'lookahead_m': 0.0, 'look_dist_m': 0.3, 'curvature': 0.0, 'strips': 4,
                 'reason': 'ok' if found else 'no line'},
        'obstacle': {'state': 'CLEAR', 'raw': 'CLEAR', 'distance_m': None},
        'decision': {'direction': 'FORWARD', 'cmd': 'F', 'track': 'OK', 'boost': False,
                     'emergency': False, 'text': 'go', 'v': 0.2, 'omega': 0.0},
        'wheels': {'pwm_l': 100, 'pwm_r': 100, 'applied_l': 100, 'applied_r': 100,
                   'tgt_l_mps': 0.2, 'tgt_r_mps': 0.2, 'meas_l_mps': 0.19,
                   'meas_r_mps': 0.2, 'closed_loop': False},
    }
    if not found:
        row['problem'] = 'line_lost'
    if i == 5:
        row['mark'] = 'bad detection'
    if i in (3, 5):
        row['views'] = f'views/{i:06d}.jpg'
    return row


def make_v0_session(root: str, name: str = '20261018_120000', n: int = 10) -> str:
    """Write a v0 recording by hand (frames/, views/, log/events/meta/summary)."""
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, 'frames'), exist_ok=True)
    os.makedirs(os.path.join(d, 'views'), exist_ok=True)
    with open(os.path.join(d, 'meta.json'), 'w') as fh:
        json.dump({'session': name, 'started': 1000.0, 'started_iso': '2026-10-18 12:00:00',
                   'rate_hz': 5.0, 'views_every_frame': False, 'config': {}}, fh)
    rows = [_row(i, n) for i in range(n)]
    with open(os.path.join(d, 'log.jsonl'), 'w') as fh:
        for r in rows:
            fh.write(json.dumps(r) + '\n')
    with open(os.path.join(d, 'events.jsonl'), 'w') as fh:
        fh.write(json.dumps({'t': 1000.0, 'dt': 0.0, 'i': 0, 'kind': 'record_start',
                             'text': 'recording at 5 fps'}) + '\n')
    for r in rows:
        cv2.imwrite(os.path.join(d, r['file']), synthetic_frame(r['i'], 320, 180))
        if 'views' in r:
            cv2.imwrite(os.path.join(d, r['views']), synthetic_frame(r['i'], 320, 180))
    with open(os.path.join(d, 'summary.json'), 'w') as fh:
        json.dump({'session': name, 'frames': n, 'problems': sum('problem' in r for r in rows),
                   'marks': 1, 'dropped': 0, 'size_mb': 0.0, 'duration_s': n * 0.2,
                   'stop_reason': 'test'}, fh)
    return d


def make_v1_session(root: str, name: str = '20261018_130000', n: int = 12) -> str:
    """Write a v1 (sharded) recording by hand.

    Rows 0..n-1 are split over two shards (even/odd halves) and written out of
    order; shard s0001's log.jsonl ends with a truncated line. Frames are real
    JPEGs; the truncated line's frame exists but has no readable row.
    """
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    half = n // 2
    parts = {0: list(range(half)), 1: list(range(half, n))}
    # out of order: second shard holds the later rows, first shard reversed
    parts[0].reverse()
    shard_man = []
    total_frames = 0
    kept = 0
    for s, idxs in parts.items():
        sname = f's{s:04d}'
        sd = os.path.join(d, 'shards', sname)
        os.makedirs(os.path.join(sd, 'frames'), exist_ok=True)
        rows = []
        for i in idxs:
            r = _row(i, n)
            r.pop('views', None)
            r['file'] = f'shards/{sname}/frames/{i:06d}.jpg'
            r['shard'] = sname
            r['save'] = 'continuous'
            r['t_mono'] = 50.0 + i * 0.2
            r['frame_age_ms'] = 12.5
            r['luma'] = 90.0
            r['wheels'].update({'rpm_l': 30.0, 'rpm_r': 31.0})
            r['enc'] = {'ticks_l': 10 * i, 'ticks_r': 10 * i, 'rel_l': 10 * i, 'rel_r': 10 * i,
                        'fault_bits': 0, 'fw': 'v3'}
            if r['line']['found']:
                r['line']['curve_px'] = [[640, 700], [650, 400], [660, 100]]
                r['line']['look_px'] = [650, 400]
            else:
                r['triggers'] = ['line_lost']
            rows.append(r)
            cv2.imwrite(os.path.join(sd, 'frames', f'{i:06d}.jpg'),
                        synthetic_frame(i, 320, 180))
        with open(os.path.join(sd, 'log.jsonl'), 'w') as fh:
            for r in rows:
                fh.write(json.dumps(r) + '\n')
            if s == 1:
                fh.write('{"i": 999, "t": 1000.0, "line": {"fo')  # truncated, no newline
        kept += len(rows)
        with open(os.path.join(sd, 'events.jsonl'), 'w') as fh:
            fh.write(json.dumps({'t': 1000.0 + idxs[0] * 0.2, 'dt': 0.0, 'i': idxs[0],
                                 'kind': 'shard_open', 'text': sname, 'shard': sname}) + '\n')
        with open(os.path.join(sd, 'telemetry.jsonl'), 'w') as fh:
            for i in idxs:
                fh.write(json.dumps({'t': 1000.0 + i * 0.2, 'seq': i, 'enabled': True}) + '\n')
        sman = {'schema': 1, 'shard': sname, 'closed': True,
                'frames': {'count': len(idxs), 'first_i': min(idxs), 'last_i': max(idxs)},
                'rows': len(idxs), 'events': 1, 'telemetry_rows': len(idxs), 'bytes': 0,
                'dropped': 0}
        with open(os.path.join(sd, 'manifest.json'), 'w') as fh:
            json.dump(sman, fh)
        shard_man.append({'name': sname, 'closed': True, 'frames': len(idxs),
                          'rows': len(idxs), 'bytes': 0})
        total_frames += len(idxs)
    with open(os.path.join(d, 'meta.json'), 'w') as fh:
        json.dump({'schema': 1, 'format': 'agv-rec', 'recorder_version': '1.0',
                   'session': name, 'started': 1000.0, 'started_iso': '2026-10-18 13:00:00',
                   'rate_hz': 5.0, 'views_every_frame': False, 'mode': 'continuous',
                   'image': {'format': 'jpeg', 'frame_w': 1280, 'frame_h': 720},
                   'config': {}}, fh)
    with open(os.path.join(d, 'manifest.json'), 'w') as fh:
        json.dump({'schema': 1, 'session': name, 'complete': True, 'updated_wall': 1100.0,
                   'shards': shard_man,
                   'totals': {'frames': total_frames, 'rows': kept, 'events': 2,
                              'telemetry_rows': kept, 'bytes': 0, 'dropped': 0}}, fh)
    return d
