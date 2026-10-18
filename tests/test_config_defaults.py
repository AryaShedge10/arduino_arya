"""Config defaults (T21): v1 recorder, auth on, list open, continuous mode."""
import _env  # noqa: F401
import os
import subprocess
import sys

CODE = ("import config;print(config.REC_FORMAT, config.REC_REQUIRE_AUTH, "
        "config.REC_LIST_REQUIRE_AUTH, config.REC_MODE_DEFAULT)")


def _cfg(**over):
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGV_REC_')}
    env.update(over)
    out = subprocess.run([sys.executable, '-c', CODE], cwd=_env.REPO, env=env,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    return out.split()


def test_clean_env_defaults():
    assert _cfg() == ['v1', 'True', 'False', 'continuous']


def test_rollback_switches():
    assert _cfg(AGV_REC_FORMAT='v0', AGV_REC_REQUIRE_AUTH='0') == ['v0', 'False', 'False', 'continuous']


if __name__ == '__main__':
    _env.run_tests(globals())
