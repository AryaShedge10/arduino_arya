"""Read-only loader for recording sessions (v0 flat layout and v1 sharded layout).

Never raises on missing/corrupt files: unparsable (e.g. truncated) JSONL lines
are skipped and absent files give empty results.
"""
import glob
import json
import os
import re

_SESSION_RE = re.compile(r'^\d{8}_\d{6}$')


def is_session_name(name) -> bool:
    return isinstance(name, str) and bool(_SESSION_RE.fullmatch(name))


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _iter_jsonl(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _shard_dirs(folder):
    folder = str(folder)
    return sorted(p for p in glob.glob(os.path.join(glob.escape(folder), 'shards', 's*'))
                  if os.path.isdir(p))


def detect_schema(folder) -> int:
    """Return 0 (v0), 1+ (from meta.json), or -1 when unknown."""
    folder = str(folder)
    meta = _read_json(os.path.join(folder, 'meta.json'))
    if meta is not None:
        s = meta.get('schema')
        if isinstance(s, int) and not isinstance(s, bool):
            return s
    if os.path.isdir(os.path.join(folder, 'shards')):
        return 1
    if os.path.isfile(os.path.join(folder, 'log.jsonl')):
        return 0
    return -1


def load_meta(folder) -> dict:
    return _read_json(os.path.join(str(folder), 'meta.json')) or {}


def _log_paths(folder):
    if detect_schema(folder) == 0:
        return [os.path.join(str(folder), 'log.jsonl')]
    paths = [os.path.join(d, 'log.jsonl') for d in _shard_dirs(folder)]
    if not paths:
        paths = [os.path.join(str(folder), 'log.jsonl')]
    return paths


def iter_rows(folder):
    for p in _log_paths(folder):
        yield from _iter_jsonl(p)


def _sort_key(r):
    i = r.get('i')
    return (0, i) if isinstance(i, (int, float)) and not isinstance(i, bool) else (1, 0)


def load_rows(folder, sort=True, *, row_from=None, row_to=None) -> list:
    """Load all rows; optionally slice to a [row_from:row_to+1] index range after sort.

    Negative indices are accepted (Python list semantics). `row_from`/`row_to`
    are silently clamped to the row list bounds; callers can compare the
    returned length against `row_to - row_from + 1` to detect out-of-range.
    """
    rows = list(iter_rows(folder))
    if sort:
        rows.sort(key=_sort_key)
    if row_from is not None or row_to is not None:
        lo = row_from if row_from is not None else 0
        hi = (row_to + 1) if row_to is not None else len(rows)
        rows = rows[lo:hi]
    return rows


def load_events(folder) -> list:
    folder = str(folder)
    paths = [os.path.join(folder, 'events.jsonl')]
    paths += [os.path.join(d, 'events.jsonl') for d in _shard_dirs(folder)]
    out = []
    for p in paths:
        out.extend(_iter_jsonl(p))
    out.sort(key=lambda e: e.get('t') if isinstance(e.get('t'), (int, float)) else 0)
    return out


def _count_frames(folder):
    """(frame count, bytes) over frames/*.jpg of the session or all shards."""
    dirs = [os.path.join(str(folder), 'frames')]
    dirs += [os.path.join(d, 'frames') for d in _shard_dirs(folder)]
    n = size = 0
    for fd in dirs:
        try:
            for e in os.scandir(fd):
                if e.name.endswith('.jpg') and not e.name.startswith('.') \
                        and e.is_file(follow_symlinks=False):
                    n += 1
                    try:
                        size += e.stat().st_size
                    except OSError:
                        pass
        except OSError:
            continue
    return n, size


def session_stats(folder) -> dict:
    folder = str(folder)
    name = os.path.basename(os.path.normpath(folder))
    data = _read_json(os.path.join(folder, 'summary.json'))
    if data is not None:
        data.setdefault('session', name)
        data['done'] = True
        return data
    man = _read_json(os.path.join(folder, 'manifest.json'))
    if man is not None and isinstance(man.get('totals'), dict):
        t = man['totals']
        return {'session': man.get('session') or name,
                'frames': t.get('frames', 0), 'problems': t.get('problems'),
                'marks': t.get('marks'), 'dropped': t.get('dropped', 0),
                'size_mb': round((t.get('bytes') or 0) / 1e6, 1),
                'done': bool(man.get('complete'))}
    n, size = _count_frames(folder)
    return {'session': name, 'frames': n, 'size_mb': round(size / 1e6, 1), 'done': False}


def frame_path(folder, row):
    """Absolute path of the row's image, only if it exists inside the session folder."""
    try:
        rel = row.get('file')
        if not isinstance(rel, str) or not rel or '\0' in rel or os.path.isabs(rel):
            return None
        root = os.path.realpath(str(folder))
        cands = [rel]
        shard = row.get('shard')
        if isinstance(shard, str) and shard and not rel.startswith('shards/'):
            cands.append(os.path.join('shards', shard, rel))
        for c in cands:
            p = os.path.realpath(os.path.join(root, c))
            if os.path.commonpath([root, p]) == root and p != root and os.path.isfile(p):
                return p
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None
