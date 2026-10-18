#!/usr/bin/env python3
"""Run every tests/test_*.py in its own process. ``--e2e`` also runs test_e2e*."""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def main() -> int:
    e2e = '--e2e' in sys.argv
    results = []
    for path in sorted(glob.glob(os.path.join(HERE, 'test_*.py'))):
        name = os.path.basename(path)
        if name.startswith('test_e2e') and not e2e:
            results.append((name, 'SKIP', 0.0, ''))
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, path], cwd=REPO, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        results.append((name, 'PASS' if p.returncode == 0 else 'FAIL', time.time() - t0, out))
    for name, st, dt, _ in results:
        print(f'{st:5} {name:36} {dt:6.1f}s')
    bad = [r for r in results if r[1] == 'FAIL']
    for name, _, _, out in bad:
        print(f'\n--- {name} ---\n{out}')
    if bad:
        print('FAILED')
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
