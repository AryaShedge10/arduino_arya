# Recorder v1 – implementation plan and todo list

Planned by Opus (architect), executed one step at a time by Sonnet (medium effort).
Backup of the tree before starting: `/home/deep/arduino_backup_before_recorder_v1/` (restore a file with `cp`).
Rule: **after every step `python3 tests/run_all.py` must print `ALL PASSED`**, and the running system must keep working.

## Todo (update the checkbox when a step is DONE and verified)

- [x] T1  Test harness + v0 baseline tests
- [x] T2  Config knobs (no behaviour change)
- [x] T3  `build_info.py`
- [x] T4  `recording_io.py` loader (v0 + v1)
- [x] T5  `replay_recording.py` uses the loader
- [x] T6  `rec_row.py` row builder + additive log fields (touches `AGV._record`)
- [x] T7  `triggers.py` + `legacy_problem`
- [x] T8  `rec_store.py` (atomic writes, shard writer, manifests)
- [x] T9  `ringbuf.py`
- [x] T10a `recorder_v1.py`: `build_meta` + `SessionWriter` thread (new, not yet used)
- [x] T10b `recorder_v1.py`: `RecorderV1` front end + decision logic (new, not yet used)
- [x] T11 `recording_tool.py` (info / verify / repair)
- [x] T12 control_state `record_mode`, API + telemetry fields
- [x] T13 `make_recorder()` facade wired into the AGV (default still v0)
- [x] T14 Dashboard Record tab: capture-mode select
- [x] T15 Web read endpoints, v1-aware, traversal-safe
- [x] T16 `derived_views.py` + endpoint
- [x] T17 `review.html` v1-aware
- [x] T18 Auth backend for recordings (file tokens, not enforced yet)
- [x] T19 Auth frontend (review page + download)
- [x] T20 Isolated end-to-end test
- [x] T20b Throttle persistent triggers (a condition that stays true must not keep the post window open)
- [x] T21 Flip defaults (`REC_FORMAT=v1`, `REC_REQUIRE_AUTH=1`)
- [x] T22 Restart the real project safely and verify
- [x] T23 README

## Facts that shape the plan
- Jetson Orin Nano, Python 3.12, OpenCV 4.6, pytest 7.4.4. **No NVENC** (no `nvv4l2h264enc`) -> video is NOT planned; per-frame JPEG stays.
- `cv2.imencode` 1280x720 q88 ~9.4 ms, `frame.copy()` ~0.4 ms; both release the GIL.
- Problems in the current v0 recorder that v1 must avoid: disk work on the control thread (`start()`, `event()`, `status()`/`tick()` call `free_gb()`), `stop()` can wait 5 s (> `TARGET_STALE_S` 0.4 s so it could stop the wheels), queue of 48 raw frames ~133 MB, `views_fn()` (hstack) on the control thread.
- `web._password_ok` counts "no credentials" as a wrong password; recording reads need their own check that does NOT touch `_fail_times` (else ~5 image loads without a token lock START out for 30 s).
- Per-frame exposure/gain is not available from OpenCV appsink; only configured values can be recorded. "Raw" frames are already flipped + green-cast corrected in `camera._reader`.
- Encoder sample time: use `ArduinoState.last_ack_monotonic`.
- Flask caches a template the first time it is rendered; a never-rendered template is read from disk, so new JS must fall back to old URLs on 404.

## Architecture decisions
**A. Format switch.** `recorder.py` keeps the current `Recorder` (v0) unchanged. New `recorder_v1.py` has `RecorderV1` (same public API). `recorder.make_recorder()` picks by `AGV_REC_FORMAT` (default `v0` until T21).
**B. Threads/memory (v1).** `tick()` (control thread) only decides, copies the frame (~0.4 ms) and does a non-blocking `put` into a bounded queue (`REC_QUEUE_MAX`, default 24). Full queue -> drop + count. Events/telemetry rows go into bounded deques drained by the worker. One `SessionWriter` thread per session does ALL disk work (folders, meta, JPEG encode, ring buffer, shards, fsync, checksums, manifests, free-disk check, summary). `stop()` only sets an Event and returns; `close(timeout)` (wait for writers) is called only from `AGV.stop()`. Session names must be unique (if equal to previous, use next second).
**C. Shards + manifest.** Shard = 300 s or 3000 frames. Layout `shards/sNNNN/{frames/NNNNNN.jpg, log.jsonl, events.jsonl, telemetry.jsonl, manifest.json}`. Images written to `frames/.NNNNNN.jpg.tmp` then `os.replace`; sha256 computed from encoded bytes in memory. JSONL append-only, flushed per line, `os.fsync` every `REC_FSYNC_S` (2 s) and at shard close. Shard close: fsync, hash JSONL from disk, write shard `manifest.json` atomically (tmp + fsync + replace + fsync dir), then rewrite top-level `manifest.json` atomically. Crash loses at most the last ~2 s of the open shard; `recording_tool.py repair` rebuilds its manifest.
**D. Ring buffer.** Inside the worker; stores already-encoded JPEG bytes + row. Bounded by age (`REC_PRE_S` 8 s) AND bytes (`REC_RING_MAX_MB` 48). In triggered mode frames are sampled into the ring at `REC_RING_HZ` (10). On trigger: write ring contents (`save="pre"`), then the trigger frame; then `save="post"` for `REC_POST_S` (3 s). Capture index `i` assigned in `tick()` (time order, may have gaps); rows may be out of order after a flush -> readers sort by `i`. Continuous mode behaves like v0.
**E. Triggers.** `triggers.py`: pure `TriggerEngine.evaluate(row, t_mono) -> list[str]`, per-trigger cooldown `REC_TRIG_COOLDOWN_S`. Triggers: `line_lost`, `track_stuck|drifting|off_line`, `obstacle_slow|stop`, `implausible` (found but |offset| or |lookahead| > `REC_TRIG_MAX_OFFSET_M`, or |angle| > `REC_TRIG_MAX_ANGLE_DEG`, strips < `REC_TRIG_MIN_STRIPS`, |curvature| > `KAPPA_MAX`), `jump` (consecutive found rows < 0.5 s apart with |d offset| > `REC_TRIG_JUMP_M` or |d angle| > `REC_TRIG_JUMP_DEG`), `enc_fault`, `link_down`. `mark` stays an operator action in the recorder. `legacy_problem(row)` reproduces today's `problem` string exactly (used by the AGV so v0 output is byte-identical).
**F. Derived views on demand (server side, `derived_views.py`).** Annotated view stays a client canvas overlay from logged `curve_px`/`look_px` (what the robot decided). Views mosaic is rebuilt on the server with the real `vision.py` (what current code sees). `Vision` is stateful -> each request uses a new `Vision()`: "cold" (default, deterministic, cacheable) or `warm=N` (<=10) replaying previous saved rows within 3 s first. Mosaic labelled with code version + cold/warm; review page warns when recorded `src_fingerprint` differs from current. One render at a time (`Semaphore(1)`, 10 s timeout else 503), LRU cache (`REC_DERIVED_CACHE`), refuse (503 "busy: rover driving") while motors are enabled unless `REC_DERIVED_WHILE_DRIVING`. Reads `Vision._dbg` directly; `vision.py` is NOT changed.
**G. Auth.** `_make_token(kind, sid, iat, exp, extra=None)`. New token kind `f` (file token): `sid` = caller's session sid, `n` = recording name, TTL `REC_FILE_TOKEN_TTL_S` (600). Logout revokes it. `POST /api/recordings/<name>/token` (needs sign-in) -> `{ft, expires_in}`. `_rec_read_ok(req, name)` accepts `?ft=`, `Bearer`, or `X-AGV-Password`; no credentials -> 401 and does NOT count as wrong password; enforced only when `REC_REQUIRE_AUTH=1`. Protects recording files, `.tar`, merged log/events/meta/manifest endpoints, derived views, `/api/build`. The `/review/<name>` HTML shell stays open (no data). Review page shares the dashboard's localStorage `agv-auth` via `templates/_auth_core.js`, shows an in-page sign-in form if needed, uses `ft` in `<img src>` and download links. List stays open (names/counts) behind `REC_LIST_REQUIRE_AUTH=0`. Path safety: strict name/file patterns + `resolve()` must stay inside `REC_DIR/<name>`.
**H. Video:** not planned.

## Data spec
Disk (v0 unchanged). v1:
```
recordings/<YYYYmmdd_HHMMSS>/
  meta.json  manifest.json  summary.json(at stop)
  shards/s0000/frames/000000.jpg ... log.jsonl events.jsonl telemetry.jsonl manifest.json
  shards/s0001/...
```
Schema detection (`recording_io.detect_schema`): `meta.json` has integer `schema` -> that; else `shards/` exists -> 1; else `log.jsonl` exists -> 0; else unknown. Readers skip unparsable lines and sort rows by `i`.

**meta.json v1** (keeps v0 keys `session, started, started_iso, rate_hz, views_every_frame(=false), config`):
`schema:1, format:"agv-rec", recorder_version:"1.0", started_mono, wall_minus_mono, mode:"continuous|triggered", capture:{baseline_hz, ring_hz, pre_s, post_s, ring_max_mb, shard_s, shard_max_frames, telem_hz, fsync_s}, image:{format:"jpeg", quality, save_width, frame_w, frame_h, post_flip, green_cast_fix}, camera:{backend, sensor_id, width, height, fps, wb_mode, wb_locked, exposure_range_ns, exposure_auto, gain, per_frame_exposure:null, flip, green_cast_ratio, pipeline}, geometry:{cam_height_m, cam_tilt_deg, cam_hfov_deg, vision_width, vision_height, bev:{x_half_m,y_near_m,y_far_m,px_per_m}, homography_vision_to_bev:3x3}, robot:{robot_id, host, wheel_diameter_m, ticks_per_m, enc_ppr, enc_gear, wheel_base_m, rover_width_m, arduino_port}, firmware:{version, ready_line}, code:{git_commit, git_dirty, git_branch, src_sha256:{file:sha}, src_fingerprint}, triggers:{cooldown_s,max_offset_m,max_angle_deg,min_strips,jump_m,jump_deg}, env:{python,opencv}, config:{...}`.

**Log row** (NEW = added in T6, also appear in v0 rows from then on; [v1] only in v1):
```
i, t, dt, file, seq, mode, enabled, speed_pct, link_ok, vision_ms, problem?, mark?, views?(v0 only)
NEW t_mono, frame_mono, frame_age_ms, closed_loop_wanted, fw_closed_ready
line:{found, offset_m, angle_deg, lookahead_m, look_dist_m, curvature, strips, reason, curve_px, look_px}
obstacle:{state, raw, distance_m, NEW enforced}
decision:{direction, cmd, track, boost, emergency, text, v, omega}
wheels:{pwm_l, pwm_r, applied_l, applied_r, tgt_l_mps, tgt_r_mps, meas_l_mps, meas_r_mps, closed_loop,
        NEW rpm_l, rpm_r, meas_tps_l, meas_tps_r, tgt_tps_l, tgt_tps_r, pwm_target_l, pwm_target_r, fw_mode}
NEW enc:{ticks_l, ticks_r, rel_l, rel_r, fault_bits, fault_l, fault_r, ack_mono, ack_age_ms, fw}
[v1] shard, save("continuous|baseline|trigger|pre|post|mark"), triggers[..], img:{w,h,bytes}, luma
```
`rpm = meas_mps / (pi*WHEEL_DIAMETER_M) * 60`. v1 `problem` = `'+'.join(triggers without mark)`.
**telemetry.jsonl [v1]** at `REC_TELEM_HZ`, every tick: `{t, t_mono, seq, enabled, mode, found, offset_m, angle_deg, obst_raw, v, omega, ticks_l, ticks_r, ack_mono, meas_l_mps, meas_r_mps, tgt_l_mps, tgt_r_mps, applied_l, applied_r, fault_bits}`.
**events.jsonl:** `{t, dt, i, kind, text}` (+ `t_mono`, `shard` in v1). Kinds: record_start, record_stop, shard_open, shard_close, mark, problem, trigger, system/camera/stm32 transitions.
**Shard manifest:** `{schema:1, shard, closed, frames:{count,first_i,last_i}, rows, events, telemetry_rows, t_mono:[a,b], t_wall:[a,b], bytes, dropped, files:{relpath:{sha256,bytes}}}`.
**Top manifest:** `{schema:1, session, complete, updated_wall, shards:[{name,closed,frames,rows,bytes,manifest_sha256}], totals:{frames,rows,events,telemetry_rows,bytes,dropped}}`.
**summary.json v1:** v0 keys (`session, frames, problems, marks, dropped, size_mb, duration_s, stop_reason`) + `schema, mode, shards, rows, complete, triggers:{name:n}, saved_by:{...}, telemetry_rows, bytes`.

## Rules for every step
- Run commands from `/home/deep/arduino`.
- Tests `import tests/_env.py` first: it points `AGV_REC_DIR`, `AGV_DASHBOARD_DIR`, `AGV_LOG_FILE` to a temp folder, sets `AGV_DASHBOARD_PASSWORD=testpw`, `AGV_CAMERA_BACKEND=fake`, `AGV_ARDUINO_PORT=/dev/agv-test-none` (no such port -> motors cannot move). Tests never touch ports 5000/5001 (use 5071/5072) and never the real `/tmp/agv-dashboard`.
- Never use `pkill -f` (kills the caller's shell). Playwright via `python3` (not `-I`); `page.goto(..., wait_until='domcontentloaded')` (endless MJPEG stream).
- Do NOT stop/restart the live project (ports 5000/5001) in any step except T22. Do NOT commit or push.
- Rollback: restore changed files from `/home/deep/arduino_backup_before_recorder_v1/` (or undo the step's edits) + the env switch where listed.

## Steps

### T1 – Test harness and v0 baseline tests (low)
New files: `tests/_env.py` (env isolation, repo on `sys.path`, `run_tests(globals())` runs `test_*` functions and prints `OK: N tests`, exit 1 on failure); `tests/fixtures.py` (`synthetic_frame(k, w=1280, h=720)`: grey floor + yellow line like `FakeCamera`; `make_v0_session(root, name, n=10)` writes the v0 layout BY HAND without importing recorder.py, some rows with `problem`, `mark`, `views`; `make_v1_session` is added in T4); `tests/test_v0_compat.py` (the current `Recorder` writes a v0 session from synthetic frames: start / 12x tick 0.25 s apart / mark / stop, expected files exist; `replay_recording.main()` on a fixture returns 0 (patch `sys.argv`); via `web.flask_app.test_client()`: `/api/recordings` lists the session, `/recordings/<n>/log.jsonl` 200, `/review/<n>` 200); `tests/run_all.py` (runs each `tests/test_*.py` as a subprocess, skips `test_e2e*` unless `--e2e`, prints PASS/FAIL table then `ALL PASSED`).
Accept: `python3 tests/test_v0_compat.py` -> `OK: 3 tests`; `python3 tests/run_all.py` -> `ALL PASSED`; `ls recordings` unchanged. Rollback: delete `tests/`.

### T2 – Config knobs (low)
`config.py`: new block after `REC_JPEG_QUALITY`; nothing reads it yet. Knobs/defaults: `REC_FORMAT` (`AGV_REC_FORMAT`, `'v0'`), `REC_MODE_DEFAULT` (`'continuous'`), `REC_SAVE_WIDTH` 0, `REC_SHARD_S` 300, `REC_SHARD_MAX_FRAMES` 3000, `REC_FSYNC_S` 2.0, `REC_QUEUE_MAX` 24, `REC_PRE_S` 8, `REC_RING_HZ` 10, `REC_RING_MAX_MB` 48, `REC_POST_S` 3, `REC_BASELINE_HZ` 0.5, `REC_TELEM_HZ` 10, `REC_TRIG_COOLDOWN_S` 1.0, `REC_TRIG_MAX_OFFSET_M` (`BEV_X_HALF_M + 0.2`), `REC_TRIG_MAX_ANGLE_DEG` 75, `REC_TRIG_MIN_STRIPS` 2, `REC_TRIG_JUMP_M` 0.15, `REC_TRIG_JUMP_DEG` 25, `REC_REQUIRE_AUTH` ('0'->bool), `REC_LIST_REQUIRE_AUTH` ('0'), `REC_FILE_TOKEN_TTL_S` 600, `REC_DERIVED_CACHE` 64, `REC_DERIVED_WHILE_DRIVING` ('0'), `ROBOT_ID` (`AGV_ROBOT_ID`, default `socket.gethostname()`). Each as `AGV_<NAME>` env var.
Accept: `python3 -c "import config;print(config.REC_FORMAT,config.REC_PRE_S,config.REC_REQUIRE_AUTH)"` -> `v0 8.0 False`; `AGV_REC_FORMAT=v1 python3 -c "import config;print(config.REC_FORMAT)"` -> `v1`; run_all green.

### T3 – `build_info.py` (low)
`get_build_info()` cached, never raises. Returns `git_commit`, `git_dirty`, `git_branch` (via `git rev-parse HEAD`, `git status --porcelain`, `git rev-parse --abbrev-ref HEAD`, each 2 s timeout, cwd = repo), `src_sha256` for vision.py, decision.py, config.py, camera.py, recorder.py, recorder_v1.py (missing files skipped), `src_fingerprint` (sha256 of sorted pairs), `host`, `robot_id`, `python`, `opencv`. Failures -> None.
Test `tests/test_build_info.py`: keys present, `git_commit` is 40 hex or None, second call does not run git again (patch `subprocess.run`, count calls). Accept `OK: 2 tests`.

### T4 – `recording_io.py` loader (low)
Functions: `detect_schema(folder)`, `load_meta`, `iter_rows(folder)` (v0: `log.jsonl`; v1: `shards/s*/log.jsonl` sorted; skip unparsable lines), `load_rows(folder, sort=True)`, `load_events`, `session_stats(folder)` (from `summary.json`, else `manifest.json`, else count `frames/*.jpg` across shards; v0-compatible keys), `frame_path(folder, row)` with containment check, `is_session_name(name)`. Add `make_v1_session` to `tests/fixtures.py`: two shards, one with a truncated last line, rows out of order, `manifest.json`.
Test `tests/test_recording_io.py`: v0/v1 detection, row counts, sort order, truncated line skipped, stats, `frame_path('../x')` -> None. Accept `OK: >=6 tests`.

### T5 – `replay_recording.py` uses the loader (low)
Replace the inline `log.jsonl` read with `recording_io.load_rows(folder)`; message when no rows becomes `no log rows in ...`; use `r.get('file')`; skip rows whose image is missing; output otherwise unchanged.
Test (add to test_recording_io.py): replay exits 0 on v0 and v1 fixtures and prints `frames replayed`. Accept: `python3 replay_recording.py recordings/<an existing session> | head -3` prints the same first lines as before the change.

### T6 – `rec_row.py` row builder + additive fields (MEDIUM: touches `AGV._record`)
New `rec_row.py`: `build_log_row(*, seq, frame_mono, t_mono, ctl, enabled, speed_pct, line, obst, enforce, d, v_t, w_t, wheels, snap, link_ok, vision_ms, curve_px, look_px, closed_wanted, fw_ready) -> dict` producing exactly the current keys/values plus the NEW fields; `build_telem_row(row) -> dict`. In `agv_drive_arduino.py` `AGV._record`, replace the inline `row = {...}` with a `build_log_row(...)` call (keep the `curve_px`/`look_px` computation where it is); `enforce = ctl['obstacle_stop']`, `fw_ready = self.arduino.closed_loop_ready()`. Nothing else changes.
Test `tests/test_rec_row.py` with SimpleNamespace stubs: every legacy key path/value equals a hard-coded expected dict; new keys present; rpm correct; json.dumps works; `build_telem_row` keys. Also new reusable `tests/test_agv_smoke.py`: starts `agv_drive_arduino.py` with the isolated env, turns recording on for ~6 s with `control_state.write_state(record=True, path=<tmp>/control.json)` then off, SIGINT to its own Popen; asserts a session exists, rows contain `enc` and `t_mono`, no `Traceback` in the temporary agv log.
Accept: tests OK, `python3 -m py_compile agv_drive_arduino.py`, smoke OK. Risk: an exception in `_record` is caught by the main loop and stops the motors (smoke test covers it).

### T7 – `triggers.py` (low-medium)
`TRIGGER_NAMES`, `legacy_problem(row)` (the exact old logic), `TriggerEngine(**overrides)` with `evaluate(row, t_mono)` and `reset()` per section E. In `AGV._record`: `problem = legacy_problem(row)` replacing the inline block.
Test `tests/test_triggers.py`: 12-case table checking `legacy_problem` against the OLD formula copied into the test; each trigger fires; cooldown suppresses repeats; `jump` needs two found rows < 0.5 s apart; `implausible` fires on a row with offset 8.12. Accept: tests OK + smoke OK.

### T8 – `rec_store.py` (low, new)
`atomic_write_bytes(path, data, fsync=False)`, `atomic_write_json(path, obj)` (tmp in same folder, fsync, `os.replace`, fsync of folder), `sha256_file`, `fsync_dir`. `ShardWriter(session_dir, index)`: `write_frame(i, jpeg_bytes) -> (rel, sha, n)` (writes `.NNNNNN.jpg.tmp` then replaces), `append_row`, `append_event`, `append_telem`, `maybe_fsync(t_mono)`, `age_s`, `frames`, `close() -> manifest dict` (also writes `manifest.json`). `write_session_manifest(session_dir, shard_manifests, complete)`.
Test `tests/test_rec_store.py`: atomic write leaves no `.tmp`; injected failure (patch `os.replace` to raise) keeps old content; manifest shas match `sha256_file`; counts correct; a reopened crashed shard (no close) still has a readable log. Accept `OK: >=5 tests`.

### T9 – `ringbuf.py` (low)
`RingBuffer(max_s, max_bytes)`: `push(t_mono, i, jpeg, row)` evicts entries older than newest-`max_s` and oldest while over the byte limit; `drain() -> list` (oldest first, empties), `nbytes`, `span_s`, `__len__`.
Test: 1000 pushes of 100 KB with `max_bytes` 5 MB keep `nbytes` <= 5 MB; age bound holds; `drain` ordered and empties; tracemalloc peak < ~1.2x `max_bytes`. Accept OK.

### T10 – `recorder_v1.py` (MEDIUM; new, not yet used)
`build_meta(...)` (pure). `SessionWriter(threading.Thread)` per section B-D: queue `get` with 0.5 s timeout; every pass drain event/telemetry deques, `maybe_fsync`, rotate shards, check free disk every 10 s; optional resize to `REC_SAVE_WIDTH`; `luma`; stop Event -> drain queue, close shard, write `summary.json` + final manifest. `RecorderV1`: same API as `Recorder`: `start(rate_hz, views, extra_meta=None, mode='continuous')`, `stop(reason)` (non-blocking), `close(timeout=5)`, `mark(note)`, `event(kind, text)` (deque only), `tick(now_wall, frame, row, problem, views_fn, *, t_mono=None, triggers=(), telem=None)` (never calls `views_fn`; decision logic per section D; continuous mode == v0 rules), `status()` (v0 keys + `format, mode, shard, ring_s, ring_mb, saved_by, telem_rows`); time/size limits checked in `tick`; free-disk flag set by the worker.
Test `tests/test_recorder_v1.py` (synthetic frames, short `REC_SHARD_S` via constructor args): continuous 5 Hz for 3 s ~15 rows and a problem row saved; triggered: a trigger at t=5 s yields >=30 `pre` rows, `post` rows, baseline rows at ~0.5 Hz; shard rotation -> >=2 shards with valid manifests; queue size 1 with slowed worker increases `dropped` while `tick` never blocks; `tick` p95 < 3 ms over 300 calls with 1280x720 frames; `stop()` returns in < 20 ms; crash (`writer._crash_for_test()` = stop without finalising) leaves loadable rows; loads with `recording_io`; telemetry rows written. Accept `OK: >=8 tests`.

### T11 – `recording_tool.py` (low)
CLI: `info <session>` (schema, counts, triggers); `verify <session>` (every manifest sha + each row's image exists; exit 0 clean / 1 problems; prints `VERIFY OK` or a list); `repair <session>` (for sessions without `summary.json`: rebuild the open shard's manifest, remove `*.tmp`, write `summary.json` with `complete:false`, `stop_reason:"recovered after crash"`). v0: `info`/`verify` of image existence only.
Test `tests/test_recording_tool.py`: crashed v1 -> verify exits 1 -> repair -> verify exits 0; a corrupted byte in one JPEG is detected; v0 fixture `info` exits 0. Accept OK.

### T12 – Control, API and status fields (low)
`control_state.py`: key `record_mode` (`'continuous'|'triggered'`, default `REC_MODE_DEFAULT`) in `read_state` (invalid -> default) and `write_state(record_mode=None)`; update docstring. `web.py` `api_record`: on start accept `"mode"`; include `mode` in the response. `telemetry.py`: add `rec_format: str = 'v0'`, `rec_mode: str = 'continuous'`, `rec_shard: str = ''`, `rec_ring_s: float = 0.0`; `web._rec_status` includes them.
Test `tests/test_control_state.py`: old control.json without the key -> `continuous`; round-trip; invalid -> default; via test_client POST `/api/record` start with mode `triggered`, GET shows `mode: triggered`. Accept OK; run_all green.

### T13 – Wire the facade into the AGV (MEDIUM-HIGH)
`recorder.py`: add `make_recorder(root=REC_DIR)` (RecorderV1 when `REC_FORMAT=='v1'`, else `Recorder`); on v0 `Recorder` only: `close(timeout=0)` no-op, `start(..., mode='continuous')` accepts and ignores `mode`, `tick(..., **_kw)`. `agv_drive_arduino.py`: `self.recorder = make_recorder()`, `self.trig = TriggerEngine()`; call `build_info.get_build_info()` once in `AGV.start()`; in `_record`: `start` passes `mode=ctl['record_mode']` and an extra meta dict (camera backend, frame size from `frame.shape`, `firmware.version/ready_line` from `snap.recent_replies`, build info); compute `triggers = self.trig.evaluate(row, now)` only when `rec.active`; pass `t_mono=now, triggers=triggers, telem=build_telem_row(row)`; `AGV.stop()`: after `stop(...)` call `self.recorder.close(5.0)`; `_rec_telemetry` new keys via `s.get(...)`.
Accept: run_all green; `python3 tests/test_agv_smoke.py` passes with the default (v0 session); `AGV_REC_FORMAT=v1 python3 tests/test_agv_smoke.py` passes (v1 session, `recording_tool verify` exits 0); the smoke test also asserts the AGV tick count with recording on is >= 90% of with it off (log counts). Rollback: `AGV_REC_FORMAT=v0` (default) or restore files.

### T14 – Dashboard Record tab (medium; template)
`templates/dashboard.html` only: `<select id="rec-mode">` (Continuous at N fps / Triggered + baseline) next to `rec-rate`; `recStart` sends `mode`; select disabled while recording; `updateRecord` shows `d.rec_mode`, `d.rec_format`, `d.rec_ring_s` (if present) in the session stat; update the help note; sessions table gets a Mode column `s.mode ?? 'continuous'`.
Test `tests/test_dashboard_ui.py` (Playwright): starts its own `web.py` (ports 5071/5072, isolated dir), `goto` with `wait_until='domcontentloaded'`, clicks the Record tab, picks Triggered, clicks Start, asserts temp `control.json` has `record_mode=triggered`, then Stop; no page errors; terminates its own web process. Accept OK.

### T15 – Web read endpoints, v1-aware (low-medium)
`web.py`: `_dir_stats` -> `recording_io.session_stats`; `_REC_FILES` regex adds `shards/s\d{4}/(frames/\d{6}\.jpg|log\.jsonl|events\.jsonl|telemetry\.jsonl|manifest\.json)` and `manifest\.json`; `recording_file` adds `target.resolve().is_relative_to(path.resolve())`; new `GET /api/recordings/<name>/log.jsonl` (merged+sorted rows streamed as JSONL, no-cache), `GET /api/recordings/<name>/events.jsonl`, `GET /api/recordings/<name>/meta` (meta + schema + summary + manifest totals); `tar` gets `--exclude=*.tmp`; read `_rec_status()` once per list call.
Test `tests/test_web_recordings.py` (test client): v0 and v1 fixtures listed with frames > 0; merged log row count equals the loader's; a shard frame is served; traversal attempts 404 (`/recordings/..%2f..%2fetc/passwd`, `/recordings/<n>/../../config.py`, `/recordings/<n>/shards/s0000/../../meta.json`, invalid name `x`, a symlink in the session pointing to `/etc/passwd`); `.tar` returns 200 and a valid tar (`tarfile.open`). Accept OK.

### T16 – `derived_views.py` + endpoint (medium)
New `derived_views.py`: `render_views(frame, warm_frames=()) -> np.ndarray` (fresh `Vision()`, `analyze(debug=True)` over warm frames then the target; 2x4 grid of BEV-sized tiles: colour, grayscale, BEV debug, yellow mask, obstacle region, obstacle mask, floor difference, legend tile with code `sha7+dirty`, cold/warm, found/offset/obstacle; "n/a" tiles when `_dbg` empty); `render_annotated(frame)` (frame with re-derived curve + lookahead point). `web.py`: `GET /api/recordings/<name>/derived/<int:i>.jpg?kind=views|annotated&warm=0..10` (row index cached by log mtime; LRU `REC_DERIVED_CACHE`; `Semaphore(1)` 10 s acquire else 503; if not `REC_DERIVED_WHILE_DRIVING` and `read_state()['enabled']` -> 503 `busy: rover driving`; headers `X-Derived-Code`, `X-Cache: hit|miss`; `vision`/`derived_views` imported lazily in the handler); `GET /api/build` -> `{git_commit, git_dirty, src_fingerprint}`.
Test `tests/test_derived_views.py`: output 1280x560 and decodes; endpoint 200 `image/jpeg`; second call `X-Cache: hit`; unknown `i` -> 404; bad kind -> 400; `enabled=True` in temp control.json -> 503; works on a v0 fixture row without `views`. Accept OK.

### T17 – `review.html` v1-aware (medium)
Load from `/api/recordings/<n>/log.jsonl`, `/events.jsonl`, `/meta`; on 404 fall back to the old `base + 'log.jsonl'` etc. Views panel: if `r.views` exists show it (v0); else load `derived/<r.i>.jpg?kind=views`, only while paused or at most every 600 ms during play; on 404/503 show the reason text; add a "warm" checkbox sending `warm=5`. Overlay scale `sx = naturalWidth / (meta.image?.frame_w || naturalWidth)`. Detail grid adds save, triggers, rpm L·R, ticks rel L·R, faults, frame age, fw, luma. Banner when `meta.code.src_fingerprint` != `/api/build` fingerprint. Header shows schema and mode.
Test `tests/test_review_ui.py` (Playwright, isolated web, auth not yet enforced): for v0 and v1 fixtures `#pos` matches `/^\d+ \/ \d+/` and is not `0 / 0`; `#frame` naturalWidth > 0; `#viewimg` naturalWidth > 0; ArrowRight changes `#pos`; no page errors. Accept OK.

### T18 – Auth backend for recordings (low while not enforced)
`web.py`: `_make_token(kind, sid, iat, exp, extra=None)` (existing tokens unchanged); `POST /api/recordings/<name>/token` (`_password_ok`; sid from the bearer payload or `'pw'` for the password header); `_rec_read_ok(req, name)` per section G applied to every endpoint listed there; list honours `REC_LIST_REQUIRE_AUTH`; helper `_rec_auth_fail` -> 401 JSON.
Test `tests/test_web_auth.py`, separate process with `AGV_REC_REQUIRE_AUTH=1`: no credentials -> 401 and `len(web._fail_times)` unchanged, then POST `/api/enable` `{enabled:true}` with `X-AGV-Password: testpw` still 200; wrong password 403; Bearer from `/api/login` 200; `ft` from the token endpoint works for img and tar; `ft` for another session 401; `ft` with TTL=1 after 2 s 401; after `/api/logout`, `ft` 401; with `REQUIRE_AUTH=0` (second subprocess) everything 200 without credentials. Accept OK.

### T19 – Auth frontend (medium)
New `templates/_auth_core.js`: `AUTH_KEY` and the `Auth` object copied from `dashboard.html` (the "Sign-in session" block), `updateAuthUi` replaced by optional `Auth.onChange`; dashboard keeps its inline copy (add "keep in sync" comment to both). `review.html`: `<script>{% include '_auth_core.js' %}</script>`; before loading `Auth.ensure()`, on failure an inline sign-in form using `Auth.login`; `getFt()` -> POST token (404 -> `ft = ''`, old-server fallback), refresh at 80% of TTL; `u(url)` appends `?ft=`; all fetches send `Auth.headers()`; on 401 run `ensure` again and show the form; download link uses a click handler with a fresh `ft`. `dashboard.html`: Download link becomes a button: `requireAuth` -> `authFetch` token -> `location.href = '/recordings/<n>.tar?ft=...'`; Review keeps `target=_blank`.
Test: extend `test_review_ui.py` with `AGV_REC_REQUIRE_AUTH=1`: no stored token -> sign-in form visible; with `page.add_init_script` setting `agv-auth` from an API login the T17 checks pass; Download returns 200 `application/x-tar` (`page.expect_download`). Accept OK.

### T20 – Isolated end-to-end test (low, test only)
`tests/test_e2e.py` (run via `run_all --e2e`) starts `web.py` (`AGV_DASHBOARD_PORT=5071 AGV_DASHBOARD_WS_PORT=5072`) and `agv_drive_arduino.py` (fake camera, port `/dev/agv-test-none`) with `AGV_REC_FORMAT=v1 AGV_REC_REQUIRE_AUTH=1 AGV_REC_SHARD_S=10`. Over HTTP: login; start triggered; wait 14 s; mark; wait 5 s; stop; poll `summary.json` up to 10 s; `recording_tool verify` exits 0; >= 2 shards; a `pre` row and a `mark` row exist; telemetry rows > 0; `replay_recording` exits 0; derived endpoint 200 (motors off); a Playwright review check; the agv log contains `Cannot connect Arduino` and no `Traceback`. Shut down its own Popens with SIGINT then TERM after 10 s. Accept `python3 tests/run_all.py --e2e` -> `ALL PASSED`.

### T21 – Flip the defaults (medium-high)
`config.py`: `AGV_REC_FORMAT` default `'v1'`, `AGV_REC_REQUIRE_AUTH` default `'1'`; `REC_MODE_DEFAULT` stays `continuous`. Accept: `run_all --e2e` green with no overrides; `AGV_REC_FORMAT=v0 python3 tests/test_agv_smoke.py` still passes. Rollback: set env vars in `agv_run.sh`, or restore config.py.

### T22 – Restart the real project safely and verify (medium)
1. `curl -s localhost:5000/api/enable`; if enabled: POST `{"enabled":false}`; then POST `/api/record` `{"action":"stop"}`.
2. Stop the AGV: `for p in $(ps -eo pid,args | awk '$2=="/usr/bin/python3" && $3=="agv_drive_arduino.py"{print $1}'); do kill -INT $p; done`; wait in a `ps` loop up to 10 s, then TERM.
3. Stop web.py the same way (`$3=="web.py"`); confirm no `agv_run.sh` copy remains. Never `pkill -f`.
4. `cd /home/deep/arduino && setsid nohup ./agv_run.sh >>logs/run.out 2>&1 </dev/null &`
5. Verify: `/status` shows `running:true`, `camera_connected:true`, `stm32_firmware_version:"v3"`, `enabled:false`; `/api/recordings` lists the old sessions; `/review/<old session>` works after sign-in; a 20 s triggered recording with motors OFF + one mark + stop; `python3 recording_tool.py verify <new>` -> `VERIFY OK`; `python3 replay_recording.py <new>` exits 0; `grep -c Traceback logs/agv.log` unchanged; (operator check, rover on blocks: manual + motor test work as before).
Rollback: revert T21 or export `AGV_REC_FORMAT=v0` and restart the same way.

### T23 – README (low)
Rewrite the `README_AGV.md` "Recording test data" section: modes, v1 layout/schema, what is NOT stored (views derived) + the cold-state caveat, ring/trigger knobs (`AGV_REC_*` table), crash safety + `recording_tool verify/repair`, auth (sign-in, `ft` links, curl with `X-AGV-Password`), v0 compatibility, the video decision. Update the `recorder.py` docstring to point to `recorder_v1.py`. Accept `grep -c AGV_REC_ README_AGV.md` >= 15.

## Owner decisions (defaults used unless the owner says otherwise)
1. Baseline commit: NOT committing (owner did not ask); backup folder used instead.
2. "Raw" frame = after software flip + green-cast fix (what the detector saw); record the flags in meta.
3. Recording list stays open (names/counts); frames/logs/views/downloads need sign-in.
4. Default capture mode stays continuous; switch to triggered later.
5. Full resolution, quality 88 (`AGV_REC_SAVE_WIDTH=0`).
6. Derived views refused while the motors are enabled.
