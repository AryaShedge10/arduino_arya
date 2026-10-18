"""recording_io: v0/v1 detection, loading, stats and safe frame paths."""
import _env  # noqa: F401  (must be first)
import json
import os

import recording_io as rio
from fixtures import make_v0_session, make_v1_session

N1 = 12


def _v0():
    return make_v0_session(_env.REC_DIR, '20261018_120000', 10)


def _v1():
    return make_v1_session(_env.REC_DIR, '20261018_130000', N1)


def test_detect_schema():
    assert rio.detect_schema(_v0()) == 0
    assert rio.detect_schema(_v1()) == 1
    empty = os.path.join(_env.REC_DIR, '20261018_140000')
    os.makedirs(empty, exist_ok=True)
    assert rio.detect_schema(empty) == -1
    assert rio.detect_schema(os.path.join(_env.REC_DIR, 'nope')) == -1
    os.makedirs(os.path.join(empty, 'shards'))
    assert rio.detect_schema(empty) == 1  # shards/ without meta


def test_v0_rows_events_meta():
    d = _v0()
    rows = rio.load_rows(d)
    assert [r['i'] for r in rows] == list(range(10))
    assert len(rio.load_events(d)) == 1
    assert rio.load_meta(d)['session'] == '20261018_120000'


def test_v1_rows_sorted_and_truncated_skipped():
    d = _v1()
    raw = [r['i'] for r in rio.iter_rows(d)]
    assert raw != sorted(raw), 'fixture rows should be out of order'
    assert len(raw) == N1 and 999 not in raw
    rows = rio.load_rows(d)
    assert [r['i'] for r in rows] == list(range(N1))
    assert [r['i'] for r in rio.load_rows(d, sort=False)] == raw


def test_v1_events_meta_and_problem_mark():
    d = _v1()
    assert len(rio.load_events(d)) == 2
    assert rio.load_meta(d)['schema'] == 1
    rows = rio.load_rows(d)
    assert any('problem' in r for r in rows) and any('mark' in r for r in rows)


def test_missing_files_never_raise():
    nope = os.path.join(_env.REC_DIR, 'missing')
    assert rio.load_rows(nope) == [] and rio.load_events(nope) == []
    assert rio.load_meta(nope) == {}
    st = rio.session_stats(nope)
    assert st['frames'] == 0 and st['done'] is False


def test_session_stats_v0_v1_and_fallbacks():
    st = rio.session_stats(_v0())
    assert st['done'] and st['frames'] == 10 and st['session'] == '20261018_120000'
    for k in ('problems', 'marks', 'dropped', 'size_mb', 'duration_s', 'stop_reason'):
        assert k in st
    d = _v1()
    st = rio.session_stats(d)
    assert st['done'] is True and st['frames'] == N1 and st['dropped'] == 0
    assert st['session'] == '20261018_130000'
    # incomplete manifest -> done False
    mp = os.path.join(d, 'manifest.json')
    m = json.load(open(mp))
    m['complete'] = False
    json.dump(m, open(mp, 'w'))
    assert rio.session_stats(d)['done'] is False
    # no manifest -> count jpgs across shards
    os.remove(mp)
    st = rio.session_stats(d)
    assert st['frames'] == N1 and st['done'] is False
    # v0 without summary -> count frames
    d0 = _v0()
    os.remove(os.path.join(d0, 'summary.json'))
    assert rio.session_stats(d0)['frames'] == 10


def test_frame_path_safe():
    d0, d1 = _v0(), _v1()
    r0 = rio.load_rows(d0)[0]
    p = rio.frame_path(d0, r0)
    assert p and os.path.isabs(p) and os.path.isfile(p)
    r1 = rio.load_rows(d1)[3]
    p = rio.frame_path(d1, r1)
    assert p and os.path.isfile(p) and 'shards' in p
    assert rio.frame_path(d1, {'file': '../x'}) is None
    assert rio.frame_path(d1, {'file': '../../etc/passwd'}) is None
    assert rio.frame_path(d1, {'file': '/etc/passwd'}) is None
    assert rio.frame_path(d1, {'file': 'frames/999999.jpg'}) is None
    assert rio.frame_path(d1, {}) is None
    assert rio.frame_path(d1, {'file': ''}) is None
    assert rio.frame_path(d1, {'file': '.'}) is None
    # symlink escaping the session
    outside = os.path.join(_env.TMP, 'secret.jpg')
    open(outside, 'wb').write(b'x')
    link = os.path.join(d0, 'frames', 'evil.jpg')
    os.symlink(outside, link)
    assert rio.frame_path(d0, {'file': 'frames/evil.jpg'}) is None
    ldir = os.path.join(d0, 'linkdir')
    os.symlink(_env.TMP, ldir)
    assert rio.frame_path(d0, {'file': 'linkdir/secret.jpg'}) is None


def test_is_session_name():
    assert rio.is_session_name('20261018_120000')
    for bad in ('', 'x', '20261018_12000', '20261018_1200000', '../20261018_120000',
                '20261018_120000\n', None, 5):
        assert not rio.is_session_name(bad), bad


if __name__ == '__main__':
    _env.run_tests(globals())
