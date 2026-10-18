"""ringbuf: age and byte bounds, drain order, oversize entry, memory."""
import _env  # noqa: F401  (must be first)
import tracemalloc

from ringbuf import RingBuffer

KB = 1024
MB = 1024 * KB


def test_byte_bound():
    limit = 5_000_000  # decimal 5 MB -> at most 48 x 100 KB
    rb = RingBuffer(1e9, limit)
    blob = b'x' * (100 * KB)
    for k in range(1000):
        rb.push(k * 0.01, k, blob, {'i': k})
        assert rb.nbytes <= limit
        assert len(rb) <= 50
    assert len(rb) == 48


def test_age_bound():
    rb = RingBuffer(8, 10 ** 9)
    for k in range(301):
        t = k * 0.1
        rb.push(t, k, b'a', None)
        assert rb.span_s <= 8.0 + 1e-9
        assert rb._d[0][0] >= t - 8 - 1e-9
    assert rb.span_s > 7.5


def test_drain_order_and_empty():
    rb = RingBuffer(100, MB)
    for k in range(10):
        rb.push(float(k), k, b'ab', k)
    out = rb.drain()
    assert [e[1] for e in out] == list(range(10))
    assert out[0] == (0.0, 0, b'ab', 0)
    assert len(rb) == 0 and rb.nbytes == 0 and rb.span_s == 0.0
    assert rb.drain() == []


def test_oversize_single_kept():
    rb = RingBuffer(10, 100)
    rb.push(0.0, 0, b'x' * 500, None)
    assert len(rb) == 1 and rb.nbytes == 500
    rb.push(1.0, 1, b'y' * 500, None)
    assert len(rb) == 1 and rb.drain()[0][1] == 1


def test_clear_and_validation():
    rb = RingBuffer(5, 1000)
    rb.push(0.0, 0, b'abc', None)
    rb.clear()
    assert len(rb) == 0 and rb.nbytes == 0
    for args in ((0, 10), (5, 0), (-1, 10)):
        try:
            RingBuffer(*args)
        except ValueError:
            continue
        raise AssertionError('no ValueError for %r' % (args,))


def test_memory_peak():
    max_bytes = 5 * MB
    rb = RingBuffer(1e9, max_bytes)
    tracemalloc.start()
    try:
        for k in range(1000):
            rb.push(k * 0.01, k, bytes([k % 256]) * (100 * KB), None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1.2 * max_bytes + 200 * KB, peak


if __name__ == '__main__':
    _env.run_tests(globals())
