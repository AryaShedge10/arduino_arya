#!/usr/bin/env python3
"""AGV camera-only runner.

Captures frames from the IMX219 through the project's own argus pipeline
(``config.py``), runs the enhanced yellow-line detector, draws a small
HUD over the detected centroid, and shows everything in a local OpenCV
window. No LiDAR, no STM32, no telemetry, no web dashboard.

Dependencies
------------
- ``/home/deep/agv/AGV`` on ``sys.path`` (set ``AGV_ROOT`` to override)
- python3-opencv from apt (has GStreamer)
- a connected IMX219 on the configured sensor-id

Usage
-----
::

    python3 agv_cam_only.py                         # local OpenCV window
    AGV_SHOW_WINDOW=0 python3 agv_cam_only.py       # headless, log only
    AGV_CAMERA_BACKEND=fake python3 agv_cam_only.py # synthetic source
    AGV_CAMERA_SENSOR_ID=1 python3 agv_cam_only.py  # CAM1 instead of CAM0
    q                                               # quit the window
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional, Tuple

# Pull in the AGV project's camera + config without touching the rest of
# the stack (no decision, lidar, stm32, telemetry, web).
AGV_ROOT = os.environ.get('AGV_ROOT', '/home/deep/agv/AGV')
if AGV_ROOT and os.path.isdir(AGV_ROOT) and AGV_ROOT not in sys.path:
    sys.path.insert(0, AGV_ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from camera import Camera, detect_yellow_centroid_enhanced  # noqa: E402
from config import (  # noqa: E402
    BILATERAL_D, BILATERAL_SIGMA_COLOR, BILATERAL_SIGMA_SPACE,
    CLAHE_CLIP_LIMIT, CLAHE_TILE_GRID,
    LINE_BORDER_IGNORE_FRAC, LINE_MIN_ASPECT_RATIO, LINE_ROI_TOP_FRACTION,
    MIN_CONTOUR_AREA, MORPHOLOGY_KERNEL,
    SHOW_WINDOW, WHITE_BALANCE,
    YELLOW_HSV_LOWER, YELLOW_HSV_LOWER_DARK,
    YELLOW_HSV_UPPER, YELLOW_HSV_UPPER_DARK,
)


def detect(frame: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """Run the enhanced yellow-line detector. Returns (cx, cy, area) or None."""
    return detect_yellow_centroid_enhanced(
        frame,
        lower=YELLOW_HSV_LOWER,
        upper=YELLOW_HSV_UPPER,
        min_area=MIN_CONTOUR_AREA,
        kernel=MORPHOLOGY_KERNEL,
        clahe_clip_limit=CLAHE_CLIP_LIMIT,
        clahe_tile_grid=CLAHE_TILE_GRID,
        white_balance=WHITE_BALANCE,
        bilateral_d=BILATERAL_D,
        bilateral_sigma_color=BILATERAL_SIGMA_COLOR,
        bilateral_sigma_space=BILATERAL_SIGMA_SPACE,
        roi_top_fraction=LINE_ROI_TOP_FRACTION,
        min_aspect_ratio=LINE_MIN_ASPECT_RATIO,
        border_ignore_frac=LINE_BORDER_IGNORE_FRAC,
        lower_dark=YELLOW_HSV_LOWER_DARK,
        upper_dark=YELLOW_HSV_UPPER_DARK,
    )


def draw_hud(frame: np.ndarray,
             centroid: Optional[Tuple[int, int, int]],
             fps: float) -> None:
    """Top status bar + highlighter disc + crosshair + centre line."""
    h, w = frame.shape[:2]

    # ── Highlighter over the detected yellow region ───────────────────
    if centroid is not None:
        cx, cy, area = centroid
        # Equivalent radius from area: a = π r² → r = √(a/π), scaled 1.4×
        # so the disc covers the whole contour with a small halo.
        r = max(20.0, (area / 3.14159) ** 0.5 * 1.4)
        overlay = frame.copy()
        cv2.circle(overlay, (cx, cy), int(r), (0, 230, 255), -1)
        cv2.addWeighted(overlay, 0.32, frame, 0.68, 0, frame)
        cv2.circle(frame, (cx, cy), int(r), (0, 0, 255), 4)
        cv2.line(frame, (cx - 18, cy), (cx + 18, cy), (255, 255, 255), 2)
        cv2.line(frame, (cx, cy - 18), (cx, cy + 18), (255, 255, 255), 2)
        cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)
        cx_err = cx - w // 2
        status = f'YELLOW  cx={cx} cy={cy} area={area} err={cx_err:+d}px'
        status_color = (0, 255, 0)
    else:
        status = 'NO LINE'
        status_color = (0, 0, 255)

    # ── Top status bar ────────────────────────────────────────────────
    cv2.rectangle(frame, (0, 0), (w, 50), (0, 0, 0), -1)
    cv2.putText(frame, status, (12, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, status_color, 2)
    cv2.putText(frame, f'{fps:4.1f} fps', (w - 140, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    # ── Vertical centre line (helps see how off-centre the line is) ──
    cv2.line(frame, (w // 2, 50), (w // 2, h), (255, 200, 0), 1)


def main() -> int:
    cam = Camera()
    if not cam.open():
        print('Camera failed to open.', file=sys.stderr)
        print('  backend:  ', cam.backend, file=sys.stderr)
        print('  pipeline: ', cam.pipeline, file=sys.stderr)
        print('  Check AGV_CAMERA_BACKEND and AGV_CAMERA_SENSOR_ID.',
              file=sys.stderr)
        return 1

    print(f'Camera opened: backend={cam.backend} connected={cam.connected}')
    if cam.pipeline:
        print('  pipeline:', cam.pipeline)

    last_tick = time.time()
    try:
        while True:
            now = time.time()
            fps = 1.0 / max(now - last_tick, 1e-6)
            last_tick = now

            info = cam.latest()
            if info is not None and info.image is not None:
                frame = info.image
                centroid = detect(frame)
                draw_hud(frame, centroid, fps)
                if SHOW_WINDOW:
                    cv2.imshow('AGV cam only  (q to quit)', frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
            else:
                # No frame yet — small sleep so we don't burn CPU
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        cam.release()
        if SHOW_WINDOW:
            cv2.destroyAllWindows()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
