"""Bounded ring buffer of already-encoded JPEG frames (pre-trigger history).

Entries are ``(t_mono, i, jpeg_bytes, row)``. The buffer is bounded by age
(entries older than ``newest_t - max_s`` are evicted) and by payload bytes
(sum of ``len(jpeg)`` <= ``max_bytes``). If a single entry is larger than
``max_bytes`` it is still kept, as the only entry.

Not thread-safe: intended for a single writer thread. Pure Python.
"""
from collections import deque


class RingBuffer:
    def __init__(self, max_s, max_bytes):
        if not max_s > 0:
            raise ValueError('max_s must be > 0')
        if not max_bytes > 0:
            raise ValueError('max_bytes must be > 0')
        self.max_s = float(max_s)
        self.max_bytes = int(max_bytes)
        self._d = deque()
        self._nbytes = 0

    def push(self, t_mono, i, jpeg, row):
        self._d.append((t_mono, i, jpeg, row))
        self._nbytes += len(jpeg)
        cutoff = t_mono - self.max_s
        d = self._d
        while len(d) > 1 and (d[0][0] < cutoff or self._nbytes > self.max_bytes):
            self._nbytes -= len(d.popleft()[2])

    def drain(self):
        out = list(self._d)
        self.clear()
        return out

    def clear(self):
        self._d.clear()
        self._nbytes = 0

    @property
    def nbytes(self):
        return self._nbytes

    @property
    def span_s(self):
        if len(self._d) < 2:
            return 0.0
        return self._d[-1][0] - self._d[0][0]

    def __len__(self):
        return len(self._d)
