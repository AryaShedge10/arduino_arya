# Phrase cache

Pre-recorded audio clips for Tarka's most frequent utterances. The
`PhraseCache` loads everything in this directory at startup, keyed by
filename stem.

## Naming convention

**The filename stem is the cache key — and the cache key is the event
name.** `motion_authorised.wav` is the clip for the `motion_authorised`
event. One file, one event. Don't introduce aliases.

```
phrases/
├── wake_ready.wav          # → "Yes?"
├── motion_authorised.wav   # → "Moving."
├── motion_complete.wav     # → "Done."
├── emergency_stop.wav      # → "Stopped."
├── voice_off.wav           # → "Voice off." (event: mute_engaged)
├── voice_on.wav            # → "Voice on."  (event: mute_released)
├── confirm_timeout.wav     # → "No confirmation. Standing by."
├── clarify_default.wav     # → "Sorry, I didn't catch that."
├── out_of_scope.wav        # → "I can't do that from voice. Please use the dashboard."
└── lidar_hold.wav          # → "Holding — obstacle ahead. Will retry when clear."
                            #   NOTE: a generic clip; the reason slot is
                            #   only spoken via Piper if/when wired in.
```

(Pin the canonical 10 first; see `tests/test_voice_persona.py` for
the locked-in wording.)

## Format

| | Recommended | Notes |
|---|---|---|
| **Container** | WAV (PCM) or Opus | WAV for editing; Opus for shipped clips. |
| **Sample rate** | 16 kHz mono | voices; we are not audiophile. |
| **Bit depth** | 16-bit | enough for speech. |
| **Opus bitrate** | 24 kbps mono | good speech quality, ~3 KB/s. |
| **Per-clip budget** | < 50 KB (Opus) | total cache stays < 250 KB. |
| **Length budget** | none over 2 s | lead with the stressed word. |

## Recording notes

- One voice actor, one take each. Consistency is the point.
- Clip `moving.wav` is the calibration — get Tarka's cadence right,
  then re-record the others until they feel like the same character.
- Listen back over the actual speaker the rover will use. Warehouse
  reverb makes intelligibility worse than your DAW does.

## How the cache uses them

```python
from voice.tts import TtsEngine

tts = TtsEngine()
tts.speak_event('motion_authorised', 'Moving.')   # plays moving.wav if present
```

If `motion_authorised.wav` is missing, the engine logs and continues
without playing — Tarka silently says nothing instead of crashing.
That makes this directory safe to ship empty during Phase 0: the
rover still detects and logs events, with audio arriving as you
record clips.
