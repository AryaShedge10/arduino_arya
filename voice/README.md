# voice — bidirectional speech for the AGV

Voice is an *input/output channel*, not a control surface. It sits beside the
dashboard and reads/writes the same `control_state.py` the dashboard does. No
new path to the motors, no new safety assumptions.

## Read first

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the design choice between
  "voice notes" and a full agent, and why the answer is *neither*.
- [`PERSONA.md`](PERSONA.md) — name, voice, personality rules. Decided up
  front so behaviour is consistent from day one.

## Layout (mirrors `lidar/`)

```
voice/
├── README.md         you are here
├── ARCHITECTURE.md   design opinion + component split
├── PERSONA.md        name, voice, three rules, sample phrases
├── _constants.py     thresholds, command vocabulary, persona name
├── __init__.py       public surface — wake/STT/intents/agent/dialogue/safety/tts/persona
├── wake.py           wake-word listener (openWakeWord / Porcupine)
├── stt.py            Whisper.cpp wrapper
├── intents.py        rules engine — primary command lane
├── agent.py          optional LLM-as-parser — secondary lane
├── dialogue.py       turn-taking, confirmation gate, slot filling
├── safety.py         voice-side clamps, mute, LiDAR override
├── tts.py            Piper wrapper + pre-baked phrase cache
└── persona.py        renders persona-appropriate utterances from intents/state
```

Tests mirror the package in `tests/test_voice_*.py`.

## Status

This directory is scaffold + documentation only. The Python modules are
designed-but-unimplemented. Build order is in `ARCHITECTURE.md`; do not start
the modules until the architecture question is settled.
