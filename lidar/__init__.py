"""Rotating LiDAR obstacle safety.

Split into three concerns so each can be read — and tested — on its own:

  ``lidar.parser``     pure RPLIDAR frame decoding, no I/O
  ``lidar.safety``     the serial port, the reader thread, the snapshot
  ``lidar.publisher``  the background ``points.json`` writer for the
                       dashboard's polar overlay

Import from the package, not the submodules::

    from lidar import LidarSafety, PointsPublisher

``serial`` is re-exported so callers can build ``SerialException``
without a second import.
"""

from __future__ import annotations

import serial  # noqa: F401  (re-exported for callers catching its errors)

from ._constants import (
    ANGLE_SCALE, DIST_Q2_TO_M, MOTOR_SPAN_MIN_DEG, NODE_LEN, NOISY_FRACTION,
    POINTS_MAX, POINTS_WINDOW_S, SCAN_HEADER, SCAN_HEADER_LEN,
    STUCK_HOLD_S, STUCK_MIN_SAMPLES, STUCK_SPREAD_M,
)
from .parser import find_scan_header, parse_nodes
from .publisher import PointsPublisher
from .safety import LidarSafety, LidarSnapshot

__all__ = [
    # safety
    'LidarSafety', 'LidarSnapshot',
    # publishing
    'PointsPublisher',
    # parsing
    'parse_nodes', 'find_scan_header',
    # protocol + thresholds
    'SCAN_HEADER_LEN', 'SCAN_HEADER', 'NODE_LEN',
    'ANGLE_SCALE', 'DIST_Q2_TO_M',
    'STUCK_SPREAD_M', 'STUCK_HOLD_S', 'STUCK_MIN_SAMPLES',
    'NOISY_FRACTION', 'MOTOR_SPAN_MIN_DEG',
    'POINTS_WINDOW_S', 'POINTS_MAX',
    'serial',
]