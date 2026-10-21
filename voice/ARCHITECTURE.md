# Architecture — voice notes vs. a "proper agent"

## TL;DR

Build **neither pure**. Build a **two-lane intent dispatcher**:

1. A **rules engine** (`intents.py`) as the *primary lane* — deterministic,
   fast, free, debuggable. Handles ~90 % of what an operator will actually say.
2. A small **LLM** (`agent.py`) as an *optional fallback parser* — used only
   when the rules engine can't match with high confidence. The LLM returns
   JSON, never touches motors.

Plus, always on:

- Wake word (`wake.py`) and STT (`stt.py`) for *incoming* speech.
- Persona + TTS (`persona.py`, `tts.py`) for *outgoing* speech.
- A confirmation gate (`dialogue.py`) so every motion command gets a spoken
  echo before wheels turn.
- Voice-side safety (`safety.py`) so LiDAR / camera obstacle state *always*
  overrides voice.

The voice package writes to `control_state.py` exactly the way `web.py`
does. There is no second path to the motors.

---

## Why "voice notes" is the wrong shape

"Voice notes" is shorthand for a STT-only pipeline that produces transcripts.
For an AGV that gives you dictation, not control. You would still need:

- a parser — natural language is rules-resistant
- a confirmation step — otherwise voice is a foot-gun
- a state machine — otherwise the rover can be told two different things at once

…and you'd have skipped the only bit people actually want: a robot that
*speaks back*. A voice layer without persona + TTS is a black box that
sometimes obeys.

## Why a "proper agent" is also the wrong shape

Putting an LLM in the driving seat on a real, mobile AGV has three real
problems:

1. **Latency.** Cloud 0.8–2 s, local 0.3–0.8 s. The robot appears drunk on
   simple commands and operators stop using it.
2. **Unpredictability.** Same prompt, different action, sometimes. A typo
   becomes a wheel command. Unacceptable with a person two metres ahead.
3. **A second control path.** If the LLM can act, you now have *two* ways to
   command the motors (dashboard, LLM). They will eventually disagree, and
   one of them does not have your clamps in front of it.

You'd end up writing the rules engine anyway, plus a validator in front of the
LLM, plus a confirmation gate — and at that point the LLM is a glorified
parser. So make it one from the start.

---

## Component split

| Module | Concern | Where it sits |
|---|---|---|
| `wake.py` | wake-word listener, always-on, low power | Jetson, foreground |
| `stt.py` | Whisper.cpp wrapper with VAD | Jetson, on-demand |
| `intents.py` | rules engine: motion, status, query, safety | primary lane |
| `agent.py` | optional LLM-as-parser, returns JSON, no motors | secondary lane |
| `dialogue.py` | turn-taking, confirm gate, slot fill, clarification | owns session state |
| `safety.py` | voice-side clamps, mute, LiDAR override, "queue not drop" | wraps `dialogue` |
| `tts.py` | Piper wrapper, pre-baked phrase cache | Jetson |
| `persona.py` | renders persona-appropriate utterances from intents + state | pure |
| `_constants.py` | thresholds, command vocab, persona name | shared |

All imports from the package, never from submodules:

```python
from voice import VoiceLoop, Intent, Persona
```

---

## Flow

```
operator ──► wake.py ──► stt.py ──► text
                                  ▼
                          intents.match(text)  ──┐
                                  │             │
                                  │ confidence  │
                                  │     ▼       │
                                  │  agent.parse │
                                  │     ▼       │
                                  ▼  Intent (JSON)
                          dialogue.confirm(intent)
                                  ▼
                          persona.utter(intent)
                                  ▼  pre-baked phrase, else Piper
                                TTS
                                  ▼
                            operator: "yes"
                                  ▼
                       control_state.write_state(...)   ◄── SAME PATH AS DASHBOARD
                                  ▼
                                motors
```

### Five hard rules (the safety contract)

1. **One path to motors.** Voice routes through `control_state.write_state`.
   Same clamps, same `MAX_SPEED_PCT`, same LiDAR settings.
2. **Confirm before motion.** Any wheel command gets a spoken echo and a
   separate confirm phrase. "Affirmative." / "Do it." / "Go." are valid; the
   prompt is varied so a stray "yes" from radio cannot authorize movement.
3. **Safety override is absolute.** LiDAR / camera obstacle state pauses
   voice-driven motion. Commands are *queued and re-announced*, never
   silently dropped: "Holding — person ahead. Will retry when clear."
4. **LLM is a parser.** Returns `{action, args, confidence}`. A code-side
   validator applies the same clamps as the dashboard. Low confidence →
   ask the operator.
5. **Hardware mute.** A physical button disables voice input. Industrial
   sites will demand this; design for it from day one.

---

## Build order

| Phase | Scope | Acceptance |
|---|---|---|
| **v0 — persona + TTS only** | Speak on existing dashboard events. No mic. | Operator hears the rover *speak naturally* during normal operation. |
| **v1 — one-way voice in** | Wake + STT + rules engine. 10–20 intents. Confirm gate. | Bench: 20 commands in noise, ≥18 correct, 2 misfires caught at confirm. |
| **v2 — two-way dialogue** | Persona-initiated speech. Clarification, fault announcements, "where to go?" | Interrupts are appropriate; never talks over itself. |
| **v3 — LLM lane on** | `agent.parse()` as fallback. Validator in place. Test harness with adversarial inputs. | Rules hit-rate ≥85 %; LLM only on misses; LLM never executes a non-validated intent. |
| **v4 — multi-person** | Who spoke? LIDAR + mic direction for "you, there — move." | Out of scope until v3 is solid. |

### What we explicitly defer

- Speaker identification (v4)
- Continuous conversation / memory (v4)
- Free-form navigation goals ("go to bay three") — needs SLAM (separate workstream)
- Cloud LLM by default — local-only first, opt-in cloud later

---

## Why this matches the rest of the project

`lidar/` decodes frames, then enforces safety, then publishes. Voice will
do the same: capture → parse → enforce → publish (a `control_state`
write). No module in `voice/` knows about the serial port, the camera, or
the dashboard template. Each can be tested in isolation, like `lidar/`.
