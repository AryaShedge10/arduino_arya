"""replay_recording.main(): v0/v1 sessions, empty folder, missing images."""
import _env  # noqa: F401  (must be first)
import contextlib
import io
import os
import sys

import replay_recording
from fixtures import make_v0_session, make_v1_session


def _run(folder):
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ['replay_recording.py', folder]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = replay_recording.main()
    finally:
        sys.argv = old
    return rc, out.getvalue(), err.getvalue()


def test_replay_v0_and_v1():
    for d in (make_v0_session(_env.REC_DIR, '20261018_120000', 6),
              make_v1_session(_env.REC_DIR, '20261018_130000', 8)):
        rc, out, err = _run(d)
        assert rc == 0, (d, rc, out, err)
        assert 'frames replayed' in out, out


def test_replay_no_log():
    d = os.path.join(_env.REC_DIR, '20261018_140000')
    os.makedirs(d, exist_ok=True)
    rc, out, err = _run(d)
    assert rc == 2 and 'no log rows in' in err


def test_replay_missing_image_skipped():
    d = make_v0_session(_env.REC_DIR, '20261018_150000', 6)
    frames = sorted(os.listdir(os.path.join(d, 'frames')))
    os.remove(os.path.join(d, 'frames', frames[0]))
    rc, out, err = _run(d)
    assert rc == 0 and '5 frames replayed' in out, (rc, out, err)


if __name__ == '__main__':
    _env.run_tests(globals())
