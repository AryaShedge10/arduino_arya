import _env
from types import SimpleNamespace as NS

import triggers
from triggers import TriggerEngine, legacy_problem


def old_formula(line, d, obst):
    problem = ''
    if not line.found:
        problem = 'line_lost'
    elif d.track in ('STUCK', 'DRIFTING', 'OFF_LINE'):
        problem = 'track_' + d.track.lower()
    if obst.raw_state != 'CLEAR':
        problem = (problem + '+' if problem else '') + 'obstacle_' + obst.raw_state.lower()
    return problem


def mkrow(found=True, track='ON_LINE', raw='CLEAR', offset=0.0, angle=0.0, look=0.0,
          curv=0.0, strips=5, fault=0, link=True):
    return {'line': {'found': found, 'offset_m': offset, 'angle_deg': angle,
                     'lookahead_m': look, 'curvature': curv, 'strips': strips},
            'decision': {'track': track}, 'obstacle': {'raw': raw},
            'enc': {'fault_bits': fault}, 'link_ok': link}


CASES = [(f, t, o) for f in (True, False)
         for t in ('ON_LINE', 'CORRECTING', 'STUCK', 'DRIFTING', 'OFF_LINE')
         for o in ('CLEAR', 'SLOW', 'STOP')]


def test_legacy_matches_old_formula():
    assert len(CASES) >= 12
    seen = set()
    for f, t, o in CASES:
        want = old_formula(NS(found=f), NS(track=t), NS(raw_state=o))
        got = legacy_problem(mkrow(found=f, track=t, raw=o))
        assert got == want, (f, t, o, got, want)
        seen.add(got)
    assert 'line_lost+obstacle_stop' in seen and 'track_stuck+obstacle_slow' in seen
    assert '' in seen


def test_each_trigger_fires():
    cases = {
        'line_lost': mkrow(found=False), 'track_stuck': mkrow(track='STUCK'),
        'track_drifting': mkrow(track='DRIFTING'), 'track_off_line': mkrow(track='OFF_LINE'),
        'obstacle_slow': mkrow(raw='SLOW'), 'obstacle_stop': mkrow(raw='STOP'),
        'implausible': mkrow(offset=8.12), 'enc_fault': mkrow(fault=1),
        'link_down': mkrow(link=False),
    }
    for name, row in cases.items():
        assert name in TriggerEngine().evaluate(row, 10.0), name
    assert TriggerEngine().evaluate(mkrow(), 10.0) == []
    assert 'implausible' in TriggerEngine().evaluate(mkrow(angle=80), 1.0)
    assert 'implausible' in TriggerEngine().evaluate(mkrow(strips=1), 1.0)
    assert 'implausible' in TriggerEngine().evaluate(mkrow(look=9), 1.0)
    assert 'implausible' in TriggerEngine().evaluate(mkrow(curv=99), 1.0)
    assert set(cases) <= set(triggers.TRIGGER_NAMES) and 'jump' in triggers.TRIGGER_NAMES


def _run(e, rows_at):
    """rows_at: iterable of (t, row); returns list of (t, fired-list)."""
    return [(t, e.evaluate(r, t)) for t, r in rows_at]


def test_cooldown_and_episode():
    e = TriggerEngine(cooldown_s=1.0)
    r = mkrow(found=False)
    assert e.evaluate(r, 10.0) == ['line_lost']
    assert e.evaluate(r, 10.5) == []
    assert e.evaluate(r, 11.1) == []          # same episode: suppressed
    e.reset()
    assert e.evaluate(r, 11.2) == ['line_lost']


def test_persistent_condition_fires_every_persist_s():
    e = TriggerEngine(persist_s=5.0, rearm_s=1.0, cooldown_s=1.0)
    fired = [t for t, f in _run(e, ((k * 0.1, mkrow(found=False)) for k in range(200))) if f]
    print('persistent firings (20 s @ 10 Hz):', len(fired), [round(t, 1) for t in fired])
    assert fired[0] == 0.0
    assert 3 <= len(fired) <= 5, fired
    assert all(4.9 <= b - a <= 5.2 for a, b in zip(fired, fired[1:])), fired
    # other persistent triggers behave the same
    for row, name in ((mkrow(link=False), 'link_down'), (mkrow(raw='SLOW'), 'obstacle_slow'),
                      (mkrow(offset=8.0), 'implausible')):
        e = TriggerEngine(persist_s=5.0)
        n = sum(1 for t, f in _run(e, ((k * 0.1, row) for k in range(200))) if name in f)
        assert 3 <= n <= 5, (name, n)


def test_default_persist_interval_is_long():
    # a condition that stays true re-fires rarely by default (30 s), so triggered mode
    # does not end up recording continuously while e.g. no line is in view
    e = TriggerEngine()
    assert e.persist_s == 30.0
    fired = [t for t, f in _run(e, ((k * 0.1, mkrow(found=False)) for k in range(600))) if f]
    assert len(fired) == 2 and fired[0] == 0.0 and 29.9 <= fired[1] <= 30.2, fired


def test_new_episode_after_rearm():
    e = TriggerEngine(persist_s=5.0, rearm_s=1.0, cooldown_s=1.0)
    lost, ok = mkrow(found=False), mkrow()
    assert e.evaluate(lost, 0.0) == ['line_lost']
    for k in range(1, 20):
        assert e.evaluate(lost, k * 0.1) == []
    t = 1.9
    while t < 3.5:                             # cleared for 1.6 s
        assert e.evaluate(ok, t) == []
        t += 0.1
    assert e.evaluate(lost, 3.5) == ['line_lost']     # immediate at the new onset
    # a cooldown larger than the gap delays the new episode until it allows
    e = TriggerEngine(persist_s=5.0, rearm_s=0.5, cooldown_s=2.0)
    assert e.evaluate(lost, 0.0) == ['line_lost']
    assert e.evaluate(ok, 0.2) == [] and e.evaluate(ok, 0.9) == []
    assert e.evaluate(lost, 0.9) == []         # new episode but cooldown holds it
    assert e.evaluate(lost, 1.5) == []
    assert e.evaluate(lost, 2.1) == ['line_lost']     # fires once cooldown allows
    assert e.evaluate(lost, 2.5) == []


def test_flicker_is_not_new_episode():
    e = TriggerEngine(persist_s=5.0, rearm_s=1.0, cooldown_s=1.0)
    lost, ok = mkrow(found=False), mkrow()
    assert e.evaluate(lost, 0.0) == ['line_lost']
    assert e.evaluate(lost, 0.9) == []
    assert e.evaluate(ok, 1.1) == [] and e.evaluate(ok, 1.2) == []
    assert e.evaluate(lost, 1.4) == []         # false for 0.4 s only
    for k in range(15, 50):
        assert e.evaluate(lost, k * 0.1) == []
    assert e.evaluate(lost, 5.0) == ['line_lost']     # periodic reminder


def test_independent_throttling():
    e = TriggerEngine(persist_s=5.0, rearm_s=1.0, cooldown_s=1.0)
    assert e.evaluate(mkrow(found=False), 0.0) == ['line_lost']
    both = mkrow(link=False)
    both['line']['found'] = False
    for k in range(1, 20):
        assert e.evaluate(mkrow(found=False), k * 0.1) == []
    assert e.evaluate(both, 2.0) == ['link_down']      # line_lost still suppressed
    for k in range(21, 50):
        assert e.evaluate(both, k * 0.1) == []
    assert e.evaluate(both, 5.0) == ['line_lost']      # its own 5 s reminder
    for k in range(51, 70):
        assert e.evaluate(both, k * 0.1) == []
    assert e.evaluate(both, 7.0) == ['link_down']


def test_jump_bookkeeping_while_suppressed():
    e = TriggerEngine(persist_s=5.0, cooldown_s=1.0, rearm_s=1.0)
    assert e.evaluate(mkrow(offset=0.0), 0.0) == []
    assert e.evaluate(mkrow(offset=0.3), 0.2) == ['jump']
    # keep jumping every 0.2 s: suppressed, but _prev follows every row
    off = 0.3
    for k in range(2, 20):
        off = 0.0 if off > 0.2 else 0.3
        assert e.evaluate(mkrow(offset=off), k * 0.2) == []
        assert e._prev[1] == off
    # small steps clear the condition for >= rearm_s, then a real jump is a new episode
    for k in range(20, 30):
        assert e.evaluate(mkrow(offset=off), k * 0.2) == []
    assert e._prev[1] == off
    assert e.evaluate(mkrow(offset=off + 0.4), 6.0) == ['jump']
    # a jump is measured against the immediately previous row, not the last firing
    e = TriggerEngine(persist_s=5.0)
    e.evaluate(mkrow(offset=0.0), 0.0)
    assert e.evaluate(mkrow(offset=0.3), 0.2) == ['jump']
    e.evaluate(mkrow(offset=0.9), 0.4)
    e.evaluate(mkrow(offset=0.9), 3.0)
    e.evaluate(mkrow(offset=0.9), 3.2)
    assert e.evaluate(mkrow(offset=0.9), 3.4) == []


def test_jump():
    e = TriggerEngine()
    assert e.evaluate(mkrow(offset=0.0), 10.0) == []
    assert e.evaluate(mkrow(offset=0.3), 10.3) == ['jump']
    e = TriggerEngine()
    e.evaluate(mkrow(offset=0.0), 10.0)
    assert e.evaluate(mkrow(offset=0.3), 10.5) == []
    e = TriggerEngine()
    e.evaluate(mkrow(angle=0), 10.0)
    assert e.evaluate(mkrow(angle=40), 10.2) == ['jump']
    # state updates even when suppressed by cooldown
    e = TriggerEngine(cooldown_s=5.0)
    e.evaluate(mkrow(offset=0.0), 1.0)
    assert e.evaluate(mkrow(offset=0.3), 1.2) == ['jump']
    assert e.evaluate(mkrow(offset=0.6), 1.4) == []
    assert e.evaluate(mkrow(offset=0.62), 1.6) == []
    assert e._prev[1] == 0.62
    # a lost row breaks the pair
    e = TriggerEngine()
    e.evaluate(mkrow(offset=0.0), 1.0)
    e.evaluate(mkrow(found=False), 1.1)
    assert e.evaluate(mkrow(offset=0.5), 1.2) == []


def test_tolerates_sparse_rows():
    e = TriggerEngine()
    for r in ({}, {'line': None}, {'t_mono': 1}, {'line': {'found': True}}):
        e.evaluate(r, 1.0)
        legacy_problem(r)
    assert e.evaluate({'line': {'found': True}}, 5.0) == []
    e.evaluate({}, None)


if __name__ == '__main__':
    _env.run_tests(globals())
