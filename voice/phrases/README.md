# Phrase cache

Pre-recorded clips of Tarka's fixed phrases. They play locally with no
network, so safety announcements ("Stopped.", "Holding — obstacle ahead.")
keep working when the internet does not. Anything not cached falls back to
live synthesis (Deepgram Aura) — and to silence if that is down too.

## Naming

**The filename stem is the event name.** `emergency_stop.wav` is the clip
for the `emergency_stop` event. One file per event; no aliases.

## Don't edit these by hand — regenerate

```bash
python -m voice.record_phrases --list      # HAVE / MISS / STALE per phrase
python -m voice.record_phrases             # record whatever is missing or stale
python -m voice.record_phrases --force     # re-record everything
python -m voice.record_phrases --only emergency_stop
```

Each clip is synthesised with the same voice as live speech, trimmed of
leading/trailing silence, and **read back through speech-to-text**; a clip the
recogniser cannot read back is not written. `manifest.json` records the exact
words each clip was made from.

**If you change a phrase in `voice/persona.py`, re-run the command.** The
clip still says the old sentence, and `tests/test_voice_phrases.py` fails
until you do. Read-back matching is automatic, not a substitute for
listening: play the clips over the rover's real speaker.

## What is *not* cached

Events whose text has a live slot — `{mode}`, `{pct}`, `{name}`, `{names}`,
`{others}`, `{greeting}` — are never recorded: a clip would play without the
slot. They are always synthesised live. (`battery_low`, `motors_enabled`,
`mode_changed`, `who_strangers`, and the `person_*` / `who_known` /
`who_mixed` family.)

`lidar_hold` is cached with its standing reason, "obstacle ahead", because
that is the only reason the event watcher ever raises it with.

## Format

Mono 16-bit WAV at Deepgram's native rate. The cache budget is ~2 MB; clips
are capped at 6 s.
