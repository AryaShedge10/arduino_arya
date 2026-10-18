#!/usr/bin/env python3
"""Re-run the CURRENT line / obstacle detector on a recording and compare it
with what was logged at the time.

    python3 replay_recording.py 20261018_141500            # by session name
    python3 replay_recording.py recordings/20261018_141500 --out replay.csv
    python3 replay_recording.py 20261018_141500 --flagged   # only problem / marked frames

Use it after changing vision.py / config.py: record once on the rover, then
check on the saved frames whether the change fixes the frames you marked
without breaking the others. No camera, Arduino or dashboard is needed.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import cv2

import recording_io
from config import REC_DIR
from vision import Vision


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session', help='session name (e.g. 20261018_141500) or its folder')
    ap.add_argument('--out', help='write a per-frame CSV here')
    ap.add_argument('--flagged', action='store_true', help='only problem / marked frames')
    args = ap.parse_args()

    folder = args.session if os.path.isdir(args.session) else os.path.join(REC_DIR, args.session)
    rows = recording_io.load_rows(folder)
    if not rows:
        print(f'no log rows in {folder}', file=sys.stderr)
        return 2
    vision = Vision()
    out_rows, d_off, d_ang = [], [], []
    old_found = new_found = changed = 0
    flagged_fixed = flagged_total = 0
    for r in rows:
        flagged = bool(r.get('problem') or r.get('mark'))
        if args.flagged and not flagged:
            continue
        img = recording_io.frame_path(folder, r)
        if img is None:
            continue
        frame = cv2.imread(img)
        if frame is None:
            continue
        v = vision.analyze(frame, debug=False)
        o = r.get('line') or {}
        n = v.line
        o_found = bool(o.get('found'))
        old_found += o_found
        new_found += bool(n.found)
        if o_found != bool(n.found):
            changed += 1
        if o_found and n.found and 'offset_m' in o and 'angle_deg' in o:
            d_off.append(abs(n.offset_m - o['offset_m']))
            d_ang.append(abs(n.angle_deg - o['angle_deg']))
        if flagged:
            flagged_total += 1
            flagged_fixed += bool(n.found) and not o_found
        out_rows.append({
            'i': r.get('i', ''), 'dt': r.get('dt', 0.0), 'flag': r.get('problem') or r.get('mark') or '',
            'old_found': o_found, 'new_found': n.found,
            'old_offset_m': o.get('offset_m', ''), 'new_offset_m': round(n.offset_m, 4),
            'old_angle_deg': o.get('angle_deg', ''), 'new_angle_deg': round(n.angle_deg, 2),
            'old_obstacle': (r.get('obstacle') or {}).get('raw'), 'new_obstacle': v.obstacle.raw_state,
            'new_reason': n.reason,
        })
    n_frames = len(out_rows)
    if not n_frames:
        print('no frames replayed')
        return 1
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    print(f'session {os.path.basename(folder)}: {n_frames} frames replayed')
    print(f'  line found   logged {old_found} ({100 * old_found / n_frames:.0f}%)   now {new_found} ({100 * new_found / n_frames:.0f}%)')
    print(f'  found/lost differs on {changed} frames')
    print(f'  where both found: mean |d offset| {mean(d_off) * 100:.1f} cm (max {max(d_off, default=0) * 100:.1f}), '
          f'mean |d angle| {mean(d_ang):.1f} deg (max {max(d_ang, default=0):.1f})')
    if flagged_total:
        print(f'  flagged frames: {flagged_total}; line now found on {flagged_fixed} that were lost before')
    obs_changed = sum(1 for r in out_rows if r['old_obstacle'] != r['new_obstacle'])
    print(f'  obstacle state differs on {obs_changed} frames')
    diff = [r for r in out_rows if r['old_found'] != r['new_found']][:15]
    if diff:
        print('  first frames where found/lost changed (frame #, time, old -> new):')
        for r in diff:
            print(f"    #{r['i']:>5}  {r['dt']:7.1f}s  {r['old_found']} -> {r['new_found']}  {r['flag']}")
    if args.out:
        with open(args.out, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
            w.writeheader()
            w.writerows(out_rows)
        print(f'  per-frame CSV: {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
