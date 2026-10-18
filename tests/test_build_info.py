"""build_info: keys, git format, caching, failure tolerance."""
import _env  # noqa: F401  (must be first)
import re
import subprocess
from unittest import mock

import build_info

KEYS = ('git_commit', 'git_dirty', 'git_branch', 'src_sha256', 'src_fingerprint',
        'host', 'robot_id', 'python', 'opencv')


def test_keys_and_git_format():
    info = build_info.get_build_info(refresh=True)
    for k in KEYS:
        assert k in info, k
    c = info['git_commit']
    assert c is None or re.fullmatch(r'[0-9a-f]{40}', c), c
    assert 'config.py' in info['src_sha256']


def test_cached_no_second_git_run():
    build_info.get_build_info(refresh=True)
    with mock.patch.object(subprocess, 'run') as m:
        build_info.get_build_info()
        build_info.get_build_info()
    assert m.call_count == 0, m.call_count


def test_subprocess_failure_degrades():
    with mock.patch.object(subprocess, 'run', side_effect=OSError('boom')):
        info = build_info.get_build_info(refresh=True)
    build_info.get_build_info(refresh=True)
    assert isinstance(info, dict)
    assert info['git_commit'] is None and info['git_dirty'] is None
    assert info['git_branch'] is None
    assert 'host' in info


if __name__ == '__main__':
    _env.run_tests(globals())
