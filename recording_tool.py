#!/usr/bin/env python3
"""Inspect, verify and repair AGV recording sessions (v0 flat and v1 sharded).

    python3 recording_tool.py info   <session>
    python3 recording_tool.py verify <session>
    python3 recording_tool.py repair <session>

<session> is a folder name under config.REC_DIR or a folder path.
Exit codes: 0 ok, 1 problems / refused, 2 session not found.
Stdlib + recording_io + rec_store only. Writes (repair) stay inside the session folder.
"""
import argparse
import glob
import json
import os
import sys

import config
import rec_store
import recording_io as rio

MAX_LISTED = 50
RECOVERED = 'recovered after crash'
_JSONL = ('log.jsonl', 'events.jsonl', 'telemetry.jsonl')


# ── helpers ────────────────────────────────────────────────────────────
def resolve_session(arg):
    if os.sep in arg or arg.startswith('.') or os.path.isdir(arg):
        cand = arg
    else:
        cand = os.path.join(config.REC_DIR, arg)
    return os.path.abspath(cand) if os.path.isdir(cand) else None


def _read_json(path):
    return rio._read_json(path)


def _shards(folder):
    return [os.path.basename(p) for p in rio._shard_dirs(folder)]


def _count_lines(path):
    n = 0
    try:
        with open(path, 'rb') as fh:
            for line in fh:
                if line.strip():
                    n += 1
    except OSError:
        pass
    return n


def _dir_bytes(folder):
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _frames_in(frames_dir):
    try:
        return sorted(e.name for e in os.scandir(frames_dir)
                      if e.name.endswith('.jpg') and not e.name.startswith('.')
                      and e.is_file(follow_symlinks=False))
    except OSError:
        return []


def _bad_lines(path):
    """(number of unparsable lines, whether the last one is an unterminated tail)."""
    bad, tail = 0, False
    try:
        with open(path, 'rb') as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode('utf-8', 'replace'))
                    ok = isinstance(obj, dict)
                except ValueError:
                    ok = False
                if not ok:
                    bad += 1
                    tail = not line.endswith(b'\n')
                else:
                    tail = False
    except OSError:
        pass
    return bad, tail


# ── info ───────────────────────────────────────────────────────────────
def cmd_info(folder):
    schema = rio.detect_schema(folder)
    meta = rio.load_meta(folder)
    summary = _read_json(os.path.join(folder, 'summary.json'))
    manifest = _read_json(os.path.join(folder, 'manifest.json'))
    rows = rio.load_rows(folder)
    events = rio.load_events(folder)
    if schema == 0:
        frames = len(_frames_in(os.path.join(folder, 'frames')))
        telem = 0
        shards = 0
    else:
        frames = sum(len(_frames_in(os.path.join(folder, 'shards', s, 'frames')))
                     for s in _shards(folder))
        telem = sum(_count_lines(os.path.join(folder, 'shards', s, 'telemetry.jsonl'))
                    for s in _shards(folder))
        shards = len(_shards(folder))
    problems = sum(1 for r in rows if r.get('problem'))
    marks = sum(1 for r in rows if r.get('mark'))
    trig, saved = {}, {}
    for r in rows:
        for t in r.get('triggers') or []:
            trig[t] = trig.get(t, 0) + 1
        if r.get('save'):
            saved[r['save']] = saved.get(r['save'], 0) + 1
    if summary is not None:
        complete = summary.get('complete', True) is not False and bool(summary)
        done = 'yes' if complete else 'no (recovered)'
    elif manifest is not None:
        done = 'yes' if manifest.get('complete') else 'no (no summary)'
    else:
        done = 'no (no summary/manifest)'
    size_mb = _dir_bytes(folder) / 1e6
    out = [
        'session:      %s' % os.path.basename(folder),
        'schema:       %s' % (schema if schema >= 0 else 'unknown'),
        'started:      %s' % meta.get('started_iso', '?'),
        'mode:         %s' % meta.get('mode', 'continuous' if schema >= 0 else '?'),
        'rate_hz:      %s' % meta.get('rate_hz', '?'),
        'rows:         %d' % len(rows),
        'frames:       %d' % frames,
        'shards:       %d' % shards,
        'events:       %d' % len(events),
        'telemetry:    %d' % telem,
        'problems:     %d' % problems,
        'marks:        %d' % marks,
    ]
    if trig:
        out.append('triggers:     ' + ', '.join('%s=%d' % kv for kv in sorted(trig.items())))
    if saved:
        out.append('saved_by:     ' + ', '.join('%s=%d' % kv for kv in sorted(saved.items())))
    out.append('size_mb:      %.1f' % size_mb)
    out.append('complete:     %s' % done)
    if summary is not None:
        out.append('stop_reason:  %s' % summary.get('stop_reason', '?'))
    print('\n'.join(out))
    return 0


# ── verify ─────────────────────────────────────────────────────────────
def _verify_v0(folder, problems):
    for r in rio.iter_rows(folder):
        if 'file' in r and rio.frame_path(folder, r) is None:
            problems.append('row i=%s: image missing: %s' % (r.get('i'), r.get('file')))


def _verify_v1(folder, problems, warnings):
    summary = _read_json(os.path.join(folder, 'summary.json'))
    recovered = bool(summary) and summary.get('stop_reason') == RECOVERED
    top = _read_json(os.path.join(folder, 'manifest.json'))
    listed = {}
    if top is None:
        if summary is not None:
            problems.append('manifest.json missing or unreadable')
    else:
        listed = {s.get('name'): s for s in top.get('shards', []) if isinstance(s, dict)}
    shards = _shards(folder)
    for name in listed:
        if name not in shards:
            problems.append('shard %s listed in manifest.json but missing on disk' % name)
    for s in shards:
        sd = os.path.join(folder, 'shards', s)
        mpath = os.path.join(sd, 'manifest.json')
        man = _read_json(mpath)
        if man is None:
            problems.append('shard %s has no manifest (not closed)' % s)
        else:
            if not man.get('closed'):
                problems.append('shard %s manifest says not closed' % s)
            for rel, info in sorted((man.get('files') or {}).items()):
                p = os.path.join(sd, rel)
                if not os.path.isfile(p):
                    problems.append('%s/%s: file listed in manifest is missing' % (s, rel))
                    continue
                try:
                    size = os.path.getsize(p)
                    if size != info.get('bytes'):
                        problems.append('%s/%s: size %d != manifest %s'
                                        % (s, rel, size, info.get('bytes')))
                    elif rec_store.sha256_file(p) != info.get('sha256'):
                        problems.append('%s/%s: sha256 mismatch (corrupt)' % (s, rel))
                except OSError as e:
                    problems.append('%s/%s: unreadable (%s)' % (s, rel, e))
            if top is not None:
                ent = listed.get(s)
                if ent is None:
                    problems.append('shard %s not listed in manifest.json' % s)
                else:
                    try:
                        sha = rec_store.sha256_file(mpath)
                    except OSError:
                        sha = None
                    if ent.get('manifest_sha256') != sha:
                        problems.append('shard %s manifest_sha256 does not match manifest.json' % s)
        bad, tail = _bad_lines(os.path.join(sd, 'log.jsonl'))
        if bad:
            msg = '%s/log.jsonl: %d unparsable line(s)' % (s, bad)
            if tail and bad == 1 and recovered:
                warnings.append(msg + ' (truncated tail kept by repair)')
            else:
                problems.append(msg)
    for r in rio.iter_rows(folder):
        if 'file' in r and rio.frame_path(folder, r) is None:
            problems.append('row i=%s: image missing: %s' % (r.get('i'), r.get('file')))


def cmd_verify(folder):
    schema = rio.detect_schema(folder)
    problems, warnings = [], []
    if schema < 0:
        problems.append('not a recording session (no meta/log/shards)')
    elif schema == 0:
        _verify_v0(folder, problems)
    else:
        _verify_v1(folder, problems, warnings)
    for w in warnings:
        print('warning: ' + w)
    if not problems:
        print('VERIFY OK')
        return 0
    print('VERIFY FAILED: %d problem(s)' % len(problems))
    for p in problems[:MAX_LISTED]:
        print('  ' + p)
    if len(problems) > MAX_LISTED:
        print('  ... and %d more' % (len(problems) - MAX_LISTED))
    return 1


# ── repair ─────────────────────────────────────────────────────────────
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rebuild_shard_manifest(folder, name):
    sd = os.path.join(folder, 'shards', name)
    fd = os.path.join(sd, 'frames')
    removed = 0
    for pat in (os.path.join(glob.escape(sd), '*.tmp'), os.path.join(glob.escape(sd), '.*.tmp'),
                os.path.join(glob.escape(fd), '*.tmp'), os.path.join(glob.escape(fd), '.*.tmp')):
        for p in glob.glob(pat):
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    files = {}
    frame_names = _frames_in(fd)
    idxs = []
    for fn in frame_names:
        p = os.path.join(fd, fn)
        files['frames/' + fn] = {'sha256': rec_store.sha256_file(p),
                                 'bytes': os.path.getsize(p)}
        try:
            idxs.append(int(fn[:-4]))
        except ValueError:
            pass
    counts = {}
    tm, tw = [None, None], [None, None]
    for jn in _JSONL:
        p = os.path.join(sd, jn)
        if not os.path.exists(p):
            counts[jn] = 0
            continue
        n = 0
        for obj in rio._iter_jsonl(p):
            n += 1
            for key, span in (('t_mono', tm), ('t', tw), ('t_wall', tw)):
                v = obj.get(key)
                if _num(v):
                    span[0] = v if span[0] is None else min(span[0], v)
                    span[1] = v if span[1] is None else max(span[1], v)
        counts[jn] = n
        files[jn] = {'sha256': rec_store.sha256_file(p), 'bytes': os.path.getsize(p)}
    st = os.stat(sd)
    if tm[0] is None:
        tm = [0.0, 0.0]
    if tw[0] is None:
        tw = [st.st_mtime, st.st_mtime]
    man = {
        'schema': rec_store.SCHEMA, 'shard': name, 'closed': True,
        'frames': {'count': len(frame_names), 'first_i': min(idxs) if idxs else None,
                   'last_i': max(idxs) if idxs else None},
        'rows': counts['log.jsonl'], 'events': counts['events.jsonl'],
        'telemetry_rows': counts['telemetry.jsonl'],
        't_mono': tm, 't_wall': tw,
        'bytes': sum(v['bytes'] for v in files.values()),
        'dropped': 0, 'files': files, 'recovered': True,
    }
    rec_store.atomic_write_json(os.path.join(sd, 'manifest.json'), man)
    rec_store.fsync_dir(sd)
    return man, removed


def cmd_repair(folder):
    schema = rio.detect_schema(folder)
    if schema == 0:
        print('v0 session: no manifest to rebuild; it can only be verified '
              '(run: recording_tool.py verify).')
        return 0
    if schema < 0:
        print('not a recording session; nothing to repair')
        return 1
    if os.path.exists(os.path.join(folder, 'summary.json')):
        print('session already has summary.json: nothing to repair')
        return 1
    manifests, rebuilt, removed = [], [], 0
    for s in _shards(folder):
        man = _read_json(os.path.join(folder, 'shards', s, 'manifest.json'))
        if man is None or not man.get('closed') or 'frames' not in man:
            man, rm = _rebuild_shard_manifest(folder, s)
            removed += rm
            rebuilt.append(s)
        manifests.append(man)
    top = rec_store.write_session_manifest(folder, manifests, complete=False)
    tot = top['totals']
    rows = rio.load_rows(folder)
    trig, saved = {}, {}
    for r in rows:
        for t in r.get('triggers') or []:
            trig[t] = trig.get(t, 0) + 1
        if r.get('save'):
            saved[r['save']] = saved.get(r['save'], 0) + 1
    spans = [m['t_mono'] for m in manifests if m.get('t_mono') and m['t_mono'][0]]
    walls = [m['t_wall'] for m in manifests if m.get('t_wall')]
    if spans:
        dur = max(b for _a, b in spans) - min(a for a, _b in spans)
    elif walls:
        dur = max(b for _a, b in walls) - min(a for a, _b in walls)
    else:
        dur = 0.0
    meta = rio.load_meta(folder)
    summary = {
        'session': meta.get('session') or os.path.basename(folder),
        'frames': tot['frames'],
        'problems': sum(1 for r in rows if r.get('problem')),
        'marks': sum(1 for r in rows if r.get('mark')),
        'dropped': tot['dropped'], 'size_mb': round(tot['bytes'] / 1e6, 1),
        'duration_s': round(dur, 1), 'stop_reason': RECOVERED,
        'schema': meta.get('schema', 1), 'mode': meta.get('mode', 'continuous'),
        'shards': len(manifests), 'rows': tot['rows'], 'complete': False,
        'triggers': trig, 'saved_by': saved, 'telemetry_rows': tot['telemetry_rows'],
        'bytes': tot['bytes'],
    }
    rec_store.atomic_write_json(os.path.join(folder, 'summary.json'), summary)
    print('repaired: %d shard manifest(s) rebuilt (%s), %d tmp file(s) removed, '
          'summary.json written (complete=false)'
          % (len(rebuilt), ', '.join(rebuilt) or '-', removed))
    return 0


# ── cli ────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog='recording_tool.py', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name, fn, h in (('info', cmd_info, 'show a summary of a session'),
                        ('verify', cmd_verify, 'check checksums and images'),
                        ('repair', cmd_repair, 'recover a crashed/open v1 session')):
        p = sub.add_parser(name, help=h)
        p.add_argument('session', help='name under REC_DIR or a folder path')
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    folder = resolve_session(args.session)
    if folder is None:
        print('session not found: %s' % args.session, file=sys.stderr)
        return 2
    return args.fn(folder)


if __name__ == '__main__':
    sys.exit(main())
