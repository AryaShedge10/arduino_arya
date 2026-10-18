"""Build / environment fingerprint for recordings. Cached, never raises."""
import hashlib
import os
import platform
import socket
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_FILES = ('vision.py', 'decision.py', 'config.py', 'camera.py',
              'recorder.py', 'recorder_v1.py')
_cache = None


def _git(*args):
    try:
        r = subprocess.run(['git', *args], cwd=_HERE, capture_output=True,
                           text=True, timeout=2)
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


def _compute():
    info = {'git_commit': None, 'git_dirty': None, 'git_branch': None,
            'src_sha256': {}, 'src_fingerprint': None, 'host': None,
            'robot_id': None, 'python': None, 'opencv': None}
    try:
        c = _git('rev-parse', 'HEAD')
        info['git_commit'] = c or None
        st = _git('status', '--porcelain')
        info['git_dirty'] = None if st is None else bool(st)
        info['git_branch'] = _git('rev-parse', '--abbrev-ref', 'HEAD') or None
    except Exception:
        pass
    try:
        for n in _SRC_FILES:
            p = os.path.join(_HERE, n)
            if os.path.isfile(p):
                try:
                    with open(p, 'rb') as f:
                        info['src_sha256'][n] = hashlib.sha256(f.read()).hexdigest()
                except Exception:
                    pass
        pairs = ','.join(f'{k}:{v}' for k, v in sorted(info['src_sha256'].items()))
        info['src_fingerprint'] = hashlib.sha256(pairs.encode()).hexdigest()
    except Exception:
        pass
    try:
        info['host'] = socket.gethostname()
    except Exception:
        pass
    try:
        import config
        info['robot_id'] = config.ROBOT_ID
    except Exception:
        pass
    try:
        info['python'] = platform.python_version()
    except Exception:
        pass
    try:
        import cv2
        info['opencv'] = cv2.__version__
    except Exception:
        pass
    return info


def get_build_info(refresh=False):
    """Return a dict (cached; do not mutate). Never raises."""
    global _cache
    if _cache is None or refresh:
        try:
            _cache = _compute()
        except Exception:
            _cache = {}
    return _cache
