"""Run the real AGV loop (fake camera, no Arduino) and record for a few seconds."""
import _env
import glob
import json
import os
import signal
import subprocess
import sys
import time


def _wait_log(path, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if needle in open(path, errors='replace').read():
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def test_agv_records_new_fields():
    import control_state
    log = os.environ['AGV_LOG_FILE']
    ctl = os.path.join(_env.DASH_DIR, 'control.json')
    assert control_state.CONTROL_PATH == ctl, control_state.CONTROL_PATH
    env = dict(os.environ, AGV_REC_FORMAT='v0')   # this test reads the v0 log.jsonl
    p = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'agv_drive_arduino.py')],
                         cwd=_env.REPO, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        assert p.poll() is None, 'AGV exited early'
        control_state.write_state(record=True, path=ctl)
        time.sleep(6)
        control_state.write_state(record=False, path=ctl)
        time.sleep(1.5)
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    sessions = [d for d in glob.glob(os.path.join(_env.REC_DIR, '*')) if os.path.isdir(d)]
    assert sessions, 'no recording session created'
    rows = []
    for s in sessions:
        lp = os.path.join(s, 'log.jsonl')
        if os.path.exists(lp):
            rows += [json.loads(l) for l in open(lp) if l.strip()]
    assert rows, 'no log rows'
    assert all('enc' in r and 't_mono' in r and 'rpm_l' in r['wheels'] for r in rows)
    text = open(log, errors='replace').read() if os.path.exists(log) else ''
    assert 'Traceback' not in text, text[-2000:]


def _run_agv(fmt, mode, rate=None, rec_s=6.0):
    """Run the AGV in a subprocess, record rec_s seconds, SIGINT. Returns
    (session_dirs, agv_log_text, exit_seconds, returncode)."""
    import shutil
    import control_state
    ctl = os.path.join(_env.DASH_DIR, 'control.json')
    log = os.environ['AGV_LOG_FILE']
    for d in glob.glob(os.path.join(_env.REC_DIR, '*')):
        shutil.rmtree(d, ignore_errors=True)
    try:
        os.remove(log)
    except OSError:
        pass
    env = dict(os.environ)
    env.pop('AGV_REC_FORMAT', None)
    if fmt:
        env['AGV_REC_FORMAT'] = fmt
    p = subprocess.Popen([sys.executable, os.path.join(_env.REPO, 'agv_drive_arduino.py')],
                         cwd=_env.REPO, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    exit_s = rc = None
    try:
        time.sleep(4)
        assert p.poll() is None, 'AGV exited early'
        kw = {'record_rate': rate} if rate else {}
        control_state.write_state(record=True, record_mode=mode, path=ctl, **kw)
        time.sleep(rec_s)
        control_state.write_state(record=False, path=ctl)
        time.sleep(1.5)
        t0 = time.time()
        p.send_signal(signal.SIGINT)
        rc = p.wait(10)
        exit_s = time.time() - t0
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    sessions = [d for d in glob.glob(os.path.join(_env.REC_DIR, '*')) if os.path.isdir(d)]
    text = open(log, errors='replace').read() if os.path.exists(log) else ''
    return sessions, text, exit_s, rc


def _count_lines(path):
    try:
        return sum(1 for l in open(path) if l.strip())
    except OSError:
        return 0


def test_agv_v0_explicit_session():
    import recording_io
    sessions, text, exit_s, rc = _run_agv('v0', 'continuous')
    assert len(sessions) == 1, sessions
    s = sessions[0]
    assert os.path.exists(os.path.join(s, 'log.jsonl')) and not os.path.isdir(os.path.join(s, 'shards'))
    assert recording_io.detect_schema(s) == 0
    rows = [json.loads(l) for l in open(os.path.join(s, 'log.jsonl')) if l.strip()]
    assert rows and all('enc' in r and 't_mono' in r for r in rows)
    assert 'Traceback' not in text, text[-2000:]
    assert exit_s is not None and exit_s < 10


def test_agv_v1_default_session():
    import recording_io
    sessions, text, exit_s, rc = _run_agv(None, 'continuous')
    assert len(sessions) == 1, sessions
    s = sessions[0]
    assert os.path.isdir(os.path.join(s, 'shards')) and recording_io.detect_schema(s) == 1
    assert 'Traceback' not in text, text[-2000:]
    assert exit_s is not None and exit_s < 10


def test_agv_v1_triggered_session():
    import recording_io
    import recording_tool
    sessions, text, exit_s, rc = _run_agv('v1', 'triggered')
    assert len(sessions) == 1, sessions
    s = sessions[0]
    assert os.path.isdir(os.path.join(s, 'shards')) and os.path.exists(os.path.join(s, 'manifest.json'))
    assert recording_io.detect_schema(s) == 1
    assert recording_tool.main(['verify', s]) == 0
    meta = json.load(open(os.path.join(s, 'meta.json')))
    assert meta.get('schema') == 1, meta.get('schema')
    assert meta.get('code', {}).get('git_commit'), meta.get('code')
    assert meta.get('image', {}).get('format') == 'jpeg', meta.get('image')
    tl = glob.glob(os.path.join(s, 'shards', '*', 'telemetry.jsonl'))
    assert tl and sum(_count_lines(t) for t in tl) > 0, 'no telemetry rows'
    assert 'Traceback' not in text, text[-2000:]
    assert rc is not None and exit_s < 10, (rc, exit_s)


def test_agv_v1_continuous_max_rate_does_not_starve():
    """Recording at 30 Hz must keep up with the loop. Baseline: the v0 recorder
    at the same rate on the same fake camera (v0 saves every due tick, so its
    count ~ loop rate); v1 must save >= 70% of that. Telemetry is rate-limited
    so it is not a usable tick counter."""
    import recording_tool
    dur = 6.0

    def saved_rows(sessions, shards):
        pat = ('shards', '*', 'log.jsonl') if shards else ('log.jsonl',)
        return sum(_count_lines(l) for s in sessions for l in glob.glob(os.path.join(s, *pat)))

    sessions0, text0, _, _ = _run_agv('v0', 'continuous', rate=30, rec_s=dur)
    base = saved_rows(sessions0, False)
    sessions, text, exit_s, rc = _run_agv('v1', 'continuous', rate=30, rec_s=dur)
    assert len(sessions) == 1, sessions
    assert recording_tool.main(['verify', sessions[0]]) == 0
    saved = saved_rows(sessions, True)
    print(f'  max-rate: v1 saved={saved} v0 baseline={base}')
    assert base > 30, base
    assert saved >= 0.7 * base, (saved, base)
    assert 'Traceback' not in text, text[-2000:]
    assert rc is not None and exit_s < 10


if __name__ == '__main__':
    _env.run_tests(globals())
