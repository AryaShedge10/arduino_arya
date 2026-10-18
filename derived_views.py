"""Re-derive the vision debug views for a saved frame with the CURRENT vision.py.

Pure functions, no global state, no web import. Each call builds a fresh
``Vision`` (it is stateful), so results are deterministic for a given input.
"""
import cv2
import numpy as np

from vision import Vision

_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _check(frame):
    if (not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3
            or frame.dtype != np.uint8 or frame.shape[0] < 8 or frame.shape[1] < 8):
        raise ValueError('invalid frame')


def _run(frame, warm_frames):
    vis = Vision()
    for f in warm_frames or ():
        _check(f)
        vis.analyze(f, debug=False)
    res = vis.analyze(frame, debug=True)
    return vis, res


def _cap(img, text):
    cv2.putText(img, text, (4, 14), _FONT, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (4, 14), _FONT, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def _fit(img, w, h):
    """Letter-box a BGR image into a w x h tile."""
    ih, iw = img.shape[:2]
    s = min(w / float(iw), h / float(ih))
    nw, nh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
    out = np.zeros((h, w, 3), np.uint8)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    out[y0:y0 + nh, x0:x0 + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return out


def _na(w, h):
    t = np.full((h, w, 3), 30, np.uint8)
    cv2.putText(t, 'n/a', (w // 2 - 20, h // 2), _FONT, 0.7, (150, 150, 150), 1, cv2.LINE_AA)
    return t


def _mask_tile(m, w, h):
    m = np.asarray(m)
    m8 = m.astype(np.uint8) * (255 if m.dtype == bool else 1)
    m8 = cv2.resize(m8, (w, h), interpolation=cv2.INTER_NEAREST)
    return cv2.cvtColor(m8, cv2.COLOR_GRAY2BGR)


def _version():
    try:
        from build_info import get_build_info
        b = get_build_info()
        c = (b.get('git_commit') or 'nogit')[:7]
        return c + ('+dirty' if b.get('git_dirty') else '')
    except Exception:
        return 'unknown'


def render_views(frame, warm_frames=(), label=''):
    _check(frame)
    vis, res = _run(frame, warm_frames)
    w, h = vis.bev_w, vis.bev_h
    d = vis._dbg or {}

    def get(fn):
        if not d:
            return _na(w, h)
        try:
            return fn()
        except Exception:
            return _na(w, h)

    gray = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    tiles = [
        ('camera', _fit(frame, w, h)),
        ('gray', _fit(gray, w, h)),
        ('bev', get(lambda: _fit(d['bev'], w, h))),
        ('yellow mask', get(lambda: _mask_tile(d['yellow'], w, h))),
        ('obstacle region', get(lambda: _mask_tile(d['region'], w, h))),
        ('obstacle mask', get(lambda: _mask_tile(d['obs'], w, h))),
        ('floor diff x8', get(lambda: cv2.resize(cv2.applyColorMap(
            np.clip(d['d'] * 8, 0, 255).astype(np.uint8), cv2.COLORMAP_JET), (w, h),
            interpolation=cv2.INTER_NEAREST))),
    ]
    legend = np.full((h, w, 3), 20, np.uint8)
    ln, ob = res.line, res.obstacle
    mode = f'warm {len(tuple(warm_frames or ()))}' if warm_frames else 'cold'
    lines = [f'code {_version()}', mode]
    if label:
        lines.append(str(label)[:40])
    lines += [f'line found: {ln.found}', f'offset {ln.offset_m:+.3f} m',
              f'angle {ln.angle_deg:+.1f} deg', f'look {ln.lookahead_m:+.3f} m',
              f'obstacle {ob.state} (raw {ob.raw_state})', f'distance {ob.distance_m:.2f} m']
    for k, t in enumerate(lines):
        cv2.putText(legend, t, (8, 36 + 22 * k), _FONT, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
    tiles.append(('legend', legend))
    out = [_cap(np.ascontiguousarray(t), c) for c, t in tiles]
    return np.vstack([np.hstack(out[:4]), np.hstack(out[4:])])


def render_annotated(frame):
    _check(frame)
    vis, res = _run(frame, ())
    out = frame.copy()
    h, w = out.shape[:2]
    ln = res.line
    if ln.found:
        pts = vis.world_to_frame(list(ln.curve), w, h)
        if len(pts) > 1:
            cv2.polylines(out, [np.array(pts, np.int32)], False, (0, 255, 0), 4, cv2.LINE_AA)
        if ln.look_point is not None:
            lp = vis.world_to_frame([ln.look_point], w, h)
            if lp:
                cv2.circle(out, lp[0], 14, (0, 0, 255), -1)
                cv2.circle(out, lp[0], 16, (255, 255, 255), 2)
    txt = f'found={ln.found} off {ln.offset_m:+.2f}m ang {ln.angle_deg:+.0f}deg'
    cv2.putText(out, txt, (10, 28), _FONT, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(out, txt, (10, 28), _FONT, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return out
