# Plan — a Voice tab on the AGV dashboard

Status: **plan only, nothing implemented.** Written 2026-10-22.

Goal: a read-only "Voice" tab that answers, at a glance and in detail,
*is the voice stack actually working right now, and what did it just hear?*

---

## 1. The constraint, stated plainly

The standing rule for this workstream has been **"don't make any changes to
the main project."** A dashboard tab cannot be built without touching
`web.py` and `templates/dashboard.html`.

This plan resolves that by making the main-project diff as small and as
inert as it is possible to make:

| File | Change | Size | Nature |
|---|---|---|---|
| `web.py` | **one** new route | ~18 lines | read-only `GET`, no auth needed |
| `templates/dashboard.html` | 4 insert lines | ~4 lines | nav button, 3 includes, `TAB_IDS` entry |
| `templates/_voice_tab.{html,js,css}` | **new files** | ~380 lines | new |
| `voice/status.py` | **new file** | ~230 lines | new, voice-side |
| `voice/telemetry.py` | **new file** | ~150 lines | new, voice-side |
| `voice/mic.py`, `stt.py`, `tts.py`, `explain.py`, `agent.py`, `event_watcher.py`, `run_phase0.py` | record-and-forward hooks | ~6 lines each | additive, no behaviour change |

Nothing in `control_state.py`, `config.py`, `agv_drive_arduino.py`,
`telemetry.py`, or the Drive/Chassis/Test/Record tabs changes. There is no
new POST endpoint that can move a wheel or write control state.

**This is the same shape `lidar/` already uses** — see §2.

---

## 2. Architecture: publish a file, read it over HTTP

The voice stack is a **separate process** (`python -m voice.run_phase0`),
not a library inside `web.py`. That is deliberate and it is load-bearing:

> The C270's ALSA device is **exclusive**. If `web.py` also opened
> `MicCapture`, exactly one of the two processes would win the device and
> the loser would fail *nondeterministically at startup*. And if `web.py`
> constructed its own `EventWatcher`, Tarka would say every phrase twice.

So the tab **must not** import `voice`. Instead, mirror
`lidar/publisher.py` exactly:

```
 voice process                        file                       web process
┌──────────────────┐                                        ┌──────────────────┐
│ run_phase0.py    │                                        │ web.py           │
│                  │                                        │                  │
│ VoiceTelemetry ──┼──record──► RingBuffers                 │                  │
│                  │        (in-memory, bounded)            │                  │
│ StatusPublisher ─┼──atomic write──► $TELEMETRY_DIR/      │                  │
│                  │        voice_status.json               │                  │
│                  │                    │                   │                  │
│ EventWatcher     │                    │                   │                  │
│ SttEngine        │                    │                   │                  │
│ MicCapture       │                    │                   │                  │
└──────────────────┘                    ▼                   ▼                  │
                              /tmp/agv-dashboard/    ┌────────────────────┐     │
                              │  voice_status.json ◄─┤ GET /api/voice/... │     │
                              │  points.json         │ (read + jsonify)   │     │
                              │  control.json        └────────────────────┘     │
                              └────────────────────────────────────────────────┘
```

Write-then-`os.replace()`, same as `PointsPublisher.publish_once()`. A
reader never sees a half-written file, and a crashed writer leaves the last
good payload rather than a truncated one.

**Staleness is a first-class state, not an error.** If the file is older
than `VOICE_STALE_S` (10 s), the tab says *voice process not running* in
red. This matters: the whole reason this project has a LiDAR stale detector
is that a sensor which silently stops must not look healthy. A Voice tab
that renders an empty-but-green panel when the mic is unplugged is the
same bug wearing a nicer shirt.

---

## 3. What the tab shows

Six cards, mirroring `_lidar_tab.html`'s structure and visual language.

### Card 1 — Voice health (the status bar)

Five lane chips, each `ok / degraded / down` with a reason string:

| Lane | Shows |
|---|---|
| **Mic** | `046d:0825` → card 2 → `plughw:2,0`, `arecord` present, product name |
| **STT** | backend (`deepgram` \| `google` \| `offline`), model `nova-3`, confidence floor `0.55` |
| **TTS** | cached phrases `0 / 20`, synthesis on/off, playback tool (`aplay`) |
| **Narrator** | backend `gemini`, model `gemini-3.8-flash`, rate-limited state |
| **Parse lane** | `agent.py` status — see the honest note below |

### Card 2 — Live transcript

The thing you actually asked for. Last utterance, large:

```
  "go forward two meters"
  0.99 confidence   ·   deepgram · en   ·   1.68 s   ·   14:32:07
```

plus a scrolling list of the last 25, each row showing text, confidence,
source, and latency. Confidence is rendered as a bar with the **0.55 floor
marked as a line** — so a 0.40 that's visibly *below* the gate is legible
at a glance, rather than a number you have to remember the threshold for.

### Card 3 — Microphone level meter

A live bar of peak RMS against the threshold, because **the RMS gate
detects loudness, not speech.** This is a known defect (a closing door
passes it). Showing the meter is not decoration — it's the fastest way for
an operator to see the gate firing on nothing. The card is captioned
accordingly rather than pretending to be a VAD.

### Card 4 — Narration log

Every event Tarka narrated: event name, the prose, source
(`gemini` \| `persona`), whether cooldown suppressed it, and whether audio
**actually played** (`SpokenPhrase.played`). That last field is the one
that matters — `played=False` is how you discover the phrase cache is empty
and synthesis is failing, which is currently the *normal* state.

### Card 5 — Parse lane

Raw utterance → outcome → which gate rejected it. One row per attempt,
last 25. This makes the 4-gate validator visible: an injection attempt
should appear as `rejected · gate=action_whitelist`, which is evidence the
defense works rather than a claim that it does.

**Honest label:** this lane is *currently the only lane*. The rules engine
(`voice/intents.py`) does not exist, so every spoken command routes here —
slow and rate-limited. The card header should say so until that changes.

### Card 6 — Latency

Rolling p50 / p95 for STT and parse, **with rate-limited calls counted
separately and never folded into the median.** The last benchmark was
contaminated by `429`s that surfaced as silent `None`s, which made a 4.60 s
median meaningless. Showing the count of throttled calls next to the
number is what makes this panel trustworthy rather than decorative.

Also on this card: which of the three env vars are present —
`DEEPGRAM_API_KEY ✓`, `GEMINI_API_KEY ✗` — **boolean only, never a value.**

---

## 4. Files

### New — `voice/telemetry.py`

Bounded ring buffers and a `VoiceTelemetry` object that the voice modules
call into. Buffers are `collections.deque(maxlen=N)` — fixed memory, no
unbounded growth on a device that runs for weeks.

```python
class VoiceTelemetry:
    def record_transcript(t: Transcript, latency_ms: int, below_floor: bool)
    def record_meter(peak_rms: float, speech: bool)
    def record_narration(event, text, source, played, suppressed)
    def record_parse(text, outcome, action, confidence, gate, latency_ms, throttled)
    def snapshot() -> dict          # the payload, bounded
    @property stale_s / last_update
```

Zero imports from the main project. Pure data.

### New — `voice/status.py`

`VoiceStatusPublisher` — a direct sibling of `PointsPublisher`. Same
`start()` / `stop()` / `publish_once()` / `_loop()` shape, same
`os.replace()` atomicity, same warn-throttle-at-10 Hz discipline, same
`__repr__`. Publishes `voice_status.json` on a timer.

Payload schema (versioned, with `schema: 1`):

```jsonc
{
  "schema": 1,
  "updated_at": 1750000000.0,
  "pid": 4821,
  "stale_after_s": 10.0,
  "lanes": {
    "mic":      {"ok": true, "usb_id": "046d:0825", "card": 2,
                 "device": "plughw:2,0", "product": "C270", "reason": ""},
    "stt":      {"ok": true, "backend": "deepgram", "model": "nova-3",
                 "confidence_floor": 0.55, "reason": ""},
    "tts":      {"ok": false, "cached": 0, "expected": 20,
                 "synth": true, "tool": "aplay", "reason": "0/20 phrases cached"},
    "narrator": {"ok": true, "backend": "gemini",
                 "model": "gemini-3.8-flash", "throttled": false, "reason": ""},
    "parser":   {"ok": true, "model": "gemini-3.8-flash", "reason": ""}
  },
  "keys": {"DEEPGRAM_API_KEY": true, "GEMINI_API_KEY": true},   // bools only
  "meter": {"peak_rms": 894.0, "threshold": 250.0, "speech": true},
  "transcripts": [ /* ≤25 */ ],
  "narration":   [ /* ≤25 */ ],
  "parses":      [ /* ≤25 */ ],
  "latency": {"stt_ms": {"p50": 1680, "p95": null, "throttled": 0, "n": 1},
              "parse_ms": {"p50": null, "p95": null, "throttled": 3, "n": 3}}
}
```

**Secrets never enter this file.** A test asserts that no value in the
payload has length > 64 or matches a key-like shape, so this can't rot
into a leak later.

### Modified — `voice/*.py` (additive, ~6 lines each)

Each module already *has* the information; it just doesn't keep it. The
change is a call, not a redesign.

| File | Addition |
|---|---|
| `mic.py` | `listen()` feeds the meter |
| `stt.py` | `hear()` records transcript + latency + below-floor |
| `tts.py` | `speak_event()` records played / cache_key |
| `explain.py` | `explain()` records text + source |
| `agent.py` | `_parse_new()` records outcome + gate + latency |
| `event_watcher.py` | forwards events to telemetry |
| `run_phase0.py` | construct `VoiceTelemetry` + `VoiceStatusPublisher`, `.start()` |

All guarded so `telemetry=None` is the default and the old behaviour is
unchanged when it's absent. The 232 existing tests keep passing
unmodified — that's the acceptance criterion for this phase.

### New — `web.py` route

One read-only `GET`, placed after `points_json()` (web.py:228) and
copying its error-fallback idiom verbatim:

```python
@flask_app.route('/api/voice/status', methods=['GET'])
def api_voice_status():
    """Voice pipeline telemetry. Read-only.

    Mirrors points_json(): the voice process publishes atomically and we
    just re-serve. Never returns an API key, never returns audio, and
    there is deliberately no POST — a diagnostics tab must not become a
    control surface (ARCHITECTURE.md rule #4).
    """
```

Returns 200 with a `stale: true` marker and the file's age when the
payload is missing or old. Never 500s — a missing voice process must not
break the dashboard.

### New — `templates/_voice_tab.{html,js,css}`

Structurally identical to `_lidar_tab.*`. The JS follows `_lidar_tab.js`:
`updateVoiceLive()` on the fetch, `authFetch` for anything privileged
(nothing here is), `setInterval` polling at 500 ms, and a
`_ensureVoiceInited()` lazy hook so the tab costs nothing until opened.

### Modified — `templates/dashboard.html` (4 lines)

| Line | Insert |
|---|---|
| after 203 | `{% include '_voice_tab.css' %}` |
| after 394 | `<button … data-tab="voice" id="tabbtn-voice">Voice<i class="tabdot" id="dot-voice"></i></button>` |
| after 626 | `{% include '_voice_tab.html' %}` |
| after 1182 | `{% include '_voice_tab.js' %}` |
| line 1581 | add `'voice'` to `TAB_IDS` |

Keyboard nav (`←`/`→`), `localStorage` tab persistence, and the a11y
`role="tablist"` wiring all come free from `showTab()`.

---

## 5. Safety rules for this tab

1. **Read-only. No POST.** The tab cannot command the rover. If voice
   input later needs a control, it goes through the same confirm gate as
   everything else — not through a dashboard button.
2. **No secrets.** Booleans only, enforced by test.
3. **No audio.** Transcripts yes, PCM bytes never.
4. **Transcripts are retained in the file** (≤25) and in the browser's
   memory. Local-only, never leaves the LAN. Worth knowing that operator
   speech is in there.
5. **Staleness shown, never hidden.** A green panel with no data behind it
   is the failure mode this project already has a name for.

---

## 6. Tests

Mirroring the existing three-file precedent:

| File | Covers |
|---|---|
| `tests/test_voice_telemetry.py` | ring buffers bounded, wrap correctly, never raise |
| `tests/test_voice_status.py` | atomic write, no secrets, schema complete, stale detection |
| `tests/test_web_voice_status.py` | route is GET-only; missing file → 200 with stale, not 500 |
| `tests/test_dashboard_ui_voice.py` | DOM ids present, tab wired, `TAB_IDS` contains `voice` |

Plus the architectural test already in this project's style: an AST scan
asserting `web.py`'s new route does not import `control_state` or reference
`write_state`. A diagnostics tab that can write control state would be a
third path to the motors.

---

## 7. Sequencing

Four commits, each independently shippable and each leaving the rover
working:

**A — `voice/` telemetry only.** New files + additive hooks. No main-project
change at all. *Deliverable:* a JSON file appears in `/tmp/agv-dashboard`
that you can `cat`. **This alone answers most of what you want to know.**

**B — web route.** ~18 lines in `web.py` + route tests. No UI.

**C — the tab.** Templates + `dashboard.html` + UI tests.

**D — optional: two debug buttons.** *See §8.*

---

## 8. Open questions for you

**D — debug buttons (optional, my recommendation: yes, but as a separate
commit).** Right now testing the mic means a CLI script. Two buttons would
fix that:

- **Listen now** — record 3 s, transcribe, show the result in Card 2
- **Speak test** — synthesise a fixed phrase and play it

Implemented as a `voice_command.json` file the voice process picks up on
its next tick — same direction of IPC as the status file, no sockets, no
network listener, **and it cannot touch `control_state`** because the voice
process has no code path there for a test phrase.

Cost: this is a control channel, and it is a deliberate exception to
"read-only". It only touches the mic and the speaker, never the motors. My
view is that a voice tab you can't *test* with is half a diagnostic tool —
but it's your call, and it ships separately so you can drop it.

**Two things I want on the record before building:**

1. **Card 5 will look bad, and it should.** With no rules engine, that lane
   is everything, it's slow, and it's rate-limited. The tab will make that
   obvious rather than hiding it.
2. **Card 3 will expose a real bug.** The RMS gate passes loud non-speech;
   you saw this live. Surfacing the meter is how it gets fixed rather than
   rediscovered every few weeks.

---

## 9. What this does not do

- It does not make voice usable. That still needs `voice/intents.py`.
- It does not record the 10 (now 20) canonical phrases. The cache is
  `0 / 20` and everything depends on Deepgram being up.
- It does not measure real latency — it *starts* measuring it, honestly,
  by keeping throttled calls out of the median.