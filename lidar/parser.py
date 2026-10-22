"""Pure RPLIDAR frame decoding — no serial, no threads, no state.

Kept separate from the reader so the byte-level logic can be unit
tested against synthetic buffers without a sensor.
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

from ._constants import (
    ANGLE_SCALE, DIST_Q2_TO_M, NODE_LEN, SCAN_HEADER, SCAN_HEADER_LEN,
)


def parse_nodes(buf: bytes, skip: int = SCAN_HEADER_LEN,
                now: Optional[float] = None) -> Iterator[tuple]:
    """Yield ``(timestamp, angle_deg, dist_m, dist_q2)`` for every well-formed
    5-byte RPLIDAR node in ``buf``. ``timestamp`` defaults to ``now`` for
    the whole batch; pass a per-call value when testing."""
    if now is None:
        now = time.monotonic()
    for i in range(skip, len(buf) - (NODE_LEN - 1), NODE_LEN):
        b0, b1, b2, b3, b4 = buf[i:i + NODE_LEN]
        # b0 holds the sync bit + S flag + quality; we don't use them
        # for distance, only the angle + distance fields matter.
        #
        # Angle is 15 bits of 1/64 degree: b2:0..7 carry bits 7..14 and
        # b1:1..7 carry bits 0..6. b1 bit 0 is a per-sample status flag
        # (measured always-1 on this clone) and is masked off by the
        # shift; the 0x7FFF clamp keeps the value a full turn.
        #
        # Do NOT "mask" this down with (b2 & 0xFE) — that drops bit 7 of
        # the angle, quantising the sweep to 2° and putting a hard
        # ceiling at 357.95°, which is enough to lose the last two
        # degrees of every revolution. See tests/test_lidar_safety.py.
        ang_q6 = ((b2 << 7) | (b1 >> 1)) & 0x7FFF
        angle = ang_q6 / ANGLE_SCALE
        dist_q2 = b3 | (b4 << 8)
        if dist_q2 == 0:
            # "invalid" sample: the head didn't get a return this round.
            continue
        yield (now, angle, dist_q2 * DIST_Q2_TO_M, dist_q2)


def find_scan_header(buf: bytes, start: int = 0) -> int:
    """Return the offset of the SCAN response header (A5 5A ...) or -1.

    The reader re-syncs on this once at start-up only: the two header
    bytes can appear inside normal node data, and a mid-stream resync
    would destroy the 5-byte stride.
    """
    return buf.find(SCAN_HEADER, start)


__all__ = ['parse_nodes', 'find_scan_header']