import _env  # noqa: F401  (must be first)
import glob
import json
import os
import tempfile
from unittest import mock

import recording_io as rio
import rec_store as rs


def _tmp():
    return tempfile.mkdtemp(prefix='rs-', dir=_env.TMP)


def _no_tmp(root):
    found = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith('.tmp')]
    assert not found, found


def _fill(sw, n, base=0, t0=100.0):
    for k in range(n):
        i = base + k
        sw.write_frame(i, b'\xff\xd8jpeg%d' % i)
        sw.append_row({'i': i, 't': 1000 + t0 + k, 't_mono': t0 + k})
    sw.append_event({'kind': 'x', 't_mono': t0})
    sw.append_telem({'t_mono': t0 + 0.5})
    sw.append_telem({'t_mono': t0 + 0.6})


def test_atomic_write_no_tmp():
    d = _tmp()
    p = os.path.join(d, 'a.json')
    rs.atomic_write_bytes(p, b'one')
    rs.atomic_write_json(p, {'a': 1})
    assert json.load(open(p)) == {'a': 1}
    _no_tmp(d)


def test_atomic_failure_keeps_old():
    d = _tmp()
    p = os.path.join(d, 'a.bin')
    rs.atomic_write_bytes(p, b'old', fsync=True)
    with mock.patch('os.replace', side_effect=OSError('boom')):
        try:
            rs.atomic_write_bytes(p, b'new')
            raise AssertionError('should raise')
        except OSError:
            pass
    assert open(p, 'rb').read() == b'old'
    assert os.listdir(d) == ['a.bin'], os.listdir(d)


def test_shard_manifest_and_counts():
    d = _tmp()
    sw = rs.ShardWriter(d, 3)
    _fill(sw, 4, base=10)
    assert sw.frames == 4 and sw.age_s >= 0
    man = sw.close()
    sd = os.path.join(d, 'shards', 's0003')
    assert man['shard'] == 's0003' and man['closed'] is True
    assert man['frames'] == {'count': 4, 'first_i': 10, 'last_i': 13}
    assert (man['rows'], man['events'], man['telemetry_rows']) == (4, 1, 2)
    assert man['t_mono'] == [100.0, 103.0] and man['dropped'] == 0
    assert len(man['files']) == 4 + 3
    for rel, v in man['files'].items():
        p = os.path.join(sd, rel)
        assert v['sha256'] == rs.sha256_file(p), rel
        assert v['bytes'] == os.path.getsize(p), rel
    assert man['bytes'] == sum(v['bytes'] for v in man['files'].values())
    assert json.load(open(os.path.join(sd, 'manifest.json'))) == man
    _no_tmp(d)


def test_empty_shard():
    d = _tmp()
    man = rs.ShardWriter(d, 0).close()
    assert man['frames'] == {'count': 0, 'first_i': None, 'last_i': None}
    assert man['rows'] == 0 and len(man['files']) == 3


def test_crashed_shard_readable():
    d = _tmp()
    json.dump({'schema': 1, 'session': 'x'}, open(os.path.join(d, 'meta.json'), 'w'))
    sw = rs.ShardWriter(d, 0)
    _fill(sw, 5)
    sw.maybe_fsync(1e12)
    del sw  # crash: never closed, no manifest
    assert not os.path.exists(os.path.join(d, 'shards', 's0000', 'manifest.json'))
    assert rio.detect_schema(d) == 1
    rows = rio.load_rows(d)
    assert [r['i'] for r in rows] == [0, 1, 2, 3, 4]


def test_session_manifest_totals():
    d = _tmp()
    mans = []
    for idx, (n, base) in enumerate([(3, 0), (2, 3)]):
        sw = rs.ShardWriter(d, idx)
        sw.dropped = idx + 1
        _fill(sw, n, base=base)
        mans.append(sw.close())
    top = rs.write_session_manifest(d, mans, complete=True)
    disk = json.load(open(os.path.join(d, 'manifest.json')))
    assert disk['schema'] == 1 and disk['complete'] is True
    assert disk['session'] == os.path.basename(d)
    t = disk['totals']
    assert (t['frames'], t['rows'], t['events'], t['telemetry_rows'], t['dropped']) == (5, 5, 2, 4, 3)
    assert t['bytes'] == sum(m['bytes'] for m in mans)
    for s in disk['shards']:
        assert s['manifest_sha256'] == rs.sha256_file(
            os.path.join(d, 'shards', s['name'], 'manifest.json'))
    assert top['totals'] == t
    _no_tmp(d)


def test_frame_appears_only_via_replace():
    d = _tmp()
    sw = rs.ShardWriter(d, 0)
    seen = []
    real = os.replace

    def spy(a, b):
        seen.append((os.path.exists(a), os.path.exists(b), os.path.basename(a)))
        return real(a, b)
    with mock.patch('os.replace', spy):
        rel, sha, n = sw.write_frame(7, b'data')
    assert seen == [(True, False, '.000007.jpg.tmp')], seen
    assert rel == 'shards/s0000/frames/000007.jpg' and n == 4
    # failing replace: no final file, no tmp
    with mock.patch('os.replace', side_effect=OSError('x')):
        try:
            sw.write_frame(8, b'zz')
        except OSError:
            pass
    assert sorted(os.listdir(sw.frames_dir)) == ['000007.jpg']
    assert sw.frames == 1


if __name__ == '__main__':
    _env.run_tests(globals())
