"""recording_tool: info / verify / repair on v0 and v1 sessions."""
import _env  # noqa: F401  (must be first)
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time

import recorder_v1
import recording_tool
from fixtures import make_v0_session, make_v1_session, synthetic_frame
from recorder_v1 import SessionWriter, build_meta

FRAME = synthetic_frame(0, 320, 180)
TOOL = os.path.join(_env.REPO, 'recording_tool.py')


def run(*args):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = recording_tool.main(list(args))
    return code, buf.getvalue() + err.getvalue()


def _writer_session(crash):
    root = tempfile.mkdtemp(prefix='rt-', dir=_env.TMP)
    name = '20261018_140000'
    meta = build_meta(session=name, started=time.time(), started_mono=time.monotonic(),
                      mode='continuous', rate_hz=5, capture={'shard_s': 1},
                      image={'format': 'jpeg', 'quality': 88, 'frame_w': 320, 'frame_h': 180})
    w = SessionWriter(root, name, meta, min_free_gb=0.0, shard_s=0.6, fsync_s=0.1)
    w.start()
    for i in range(10):
        w.submit({'op': 'frame', 'kind': 'continuous', 'i': i, 'frame': FRAME,
                  'row': {'i': i, 't': 1000.0 + i, 't_mono': 50.0 + i * 0.25,
                          'problem': 'line_lost' if i == 4 else ''},
                  'flush_ring': False})
        time.sleep(0.2)
    end = time.time() + 10
    while w.rows < 10 and time.time() < end:
        time.sleep(0.02)
    if crash:
        w._crash_for_test()
    else:
        w.finish('stopped')
    w.join(10)
    return os.path.join(root, name)


def test_crash_verify_repair_verify():
    d = _writer_session(crash=True)
    code, out = run('verify', d)
    assert code == 1 and 'not closed' in out, out
    code, out = run('repair', d)
    assert code == 0, out
    assert os.path.exists(os.path.join(d, 'summary.json'))
    code, out = run('verify', d)
    assert code == 0 and 'VERIFY OK' in out, out
    code, out = run('info', d)
    assert code == 0 and 'recovered after crash' in out, out
    # second repair: nothing to do, changes nothing
    before = open(os.path.join(d, 'manifest.json')).read()
    code, out = run('repair', d)
    assert code == 1 and 'nothing to repair' in out
    assert open(os.path.join(d, 'manifest.json')).read() == before


def test_repair_truncated_tail_and_tmp():
    d = _writer_session(crash=True)
    shards = sorted(os.listdir(os.path.join(d, 'shards')))
    last = os.path.join(d, 'shards', shards[-1])
    with open(os.path.join(last, 'log.jsonl'), 'ab') as fh:
        fh.write(b'{"i": 99, "t": 1.0, "li')
    open(os.path.join(last, 'frames', '.000099.jpg.tmp'), 'wb').write(b'x')
    code, out = run('repair', d)
    assert code == 0, out
    assert not os.path.exists(os.path.join(last, 'frames', '.000099.jpg.tmp'))
    code, out = run('verify', d)
    assert code == 0 and 'VERIFY OK' in out, out


def test_clean_session_verify_and_info_subprocess():
    d = _writer_session(crash=False)
    r = subprocess.run([sys.executable, TOOL, 'verify', d], capture_output=True, text=True)
    assert r.returncode == 0 and 'VERIFY OK' in r.stdout, r.stdout + r.stderr
    r = subprocess.run([sys.executable, TOOL, 'info', d], capture_output=True, text=True)
    assert r.returncode == 0 and 'schema:       1' in r.stdout and 'rows:         10' in r.stdout, r.stdout
    code, out = run('repair', d)
    assert code == 1 and 'nothing to repair' in out


def test_corrupt_jpeg_detected():
    d = _writer_session(crash=False)
    jpg = None
    for root, _dirs, files in os.walk(os.path.join(d, 'shards')):
        for f in sorted(files):
            if f.endswith('.jpg'):
                jpg = os.path.join(root, f)
                break
        if jpg:
            break
    data = bytearray(open(jpg, 'rb').read())
    data[len(data) // 2] ^= 0xFF
    open(jpg, 'wb').write(bytes(data))
    code, out = run('verify', d)
    assert code == 1 and os.path.basename(jpg) in out and 'sha256' in out, out


def test_v0_info_verify():
    root = tempfile.mkdtemp(prefix='rt0-', dir=_env.TMP)
    d = make_v0_session(root, '20261018_120000', n=8)
    code, out = run('info', d)
    assert code == 0 and 'schema:       0' in out, out
    code, out = run('verify', d)
    assert code == 0 and 'VERIFY OK' in out, out
    code, out = run('repair', d)
    assert code == 0 and 'v0' in out
    os.remove(os.path.join(d, 'frames', '000002.jpg'))
    code, out = run('verify', d)
    assert code == 1 and '000002.jpg' in out, out


def test_v1_fixture_info_and_name_lookup():
    d = make_v1_session(_env.REC_DIR, '20261018_130000')
    code, out = run('info', '20261018_130000')   # name under REC_DIR
    assert code == 0 and 'shards:       2' in out, out
    code, out = run('verify', d)
    assert code == 1   # fixture manifests carry no sha/files and a truncated line


def test_unknown_session():
    code, out = run('info', 'no_such_session')
    assert code == 2
    code, out = run('verify', os.path.join(_env.TMP, 'nope'))
    assert code == 2
    r = subprocess.run([sys.executable, TOOL, 'repair', 'zzz'], capture_output=True, text=True)
    assert r.returncode == 2


if __name__ == '__main__':
    _env.run_tests(globals())
