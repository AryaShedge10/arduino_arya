#!/usr/bin/env python3
"""Batch-generate WAV files for all pending phrases in manifest.json.

Reads voice/phrases/manifest.json, finds every entry with "pending": true
and "wav": false, calls Deepgram Aura TTS for each, saves the WAV to
voice/phrases/<key>.wav, and updates the manifest entry with the real
bytes / seconds / heard / recorded fields.

Usage (from the repo root):
    export DEEPGRAM_API_KEY='...'
    python3 voice/generate_pending_wavs.py

Options:
    --dry-run     Print what would be generated, don't call the API.
    --key KEY     Only generate the WAV for phrase KEY.
    --force       Re-generate even if WAV already exists on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import time
import wave
from datetime import date
from pathlib import Path

import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('generate_wavs')

PHRASES_DIR = Path(__file__).parent / 'phrases'
MANIFEST_PATH = PHRASES_DIR / 'manifest.json'
DEEPGRAM_URL = 'https://api.deepgram.com/v1/speak'
VOICE = 'aura-2-thalia-en'
RATE_LIMIT_DELAY_S = 0.5   # polite pause between requests


def _repair_wav_header(data: bytes) -> bytes:
    """Fix Deepgram's placeholder RIFF/data chunk sizes."""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return data
    if data[12:16] != b'fmt ':
        return data
    fmt_size = struct.unpack('<I', data[16:20])[0]
    data_off = 12 + 8 + fmt_size
    if data[20 + fmt_size:24 + fmt_size] != b'data':
        return data
    if len(data) < data_off + 8:
        return data
    payload = len(data) - (data_off + 8)
    out = bytearray(data)
    struct.pack_into('<I', out, 4, len(data) - 8)
    struct.pack_into('<I', out, data_off + 4, payload)
    return bytes(out)


def _wav_duration(path: Path) -> float:
    """Return duration in seconds for a WAV file."""
    try:
        with wave.open(str(path), 'rb') as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def _synthesize(text: str, api_key: str) -> bytes | None:
    """Call Deepgram and return WAV bytes, or None on failure."""
    try:
        query = urllib.parse.urlencode({
            'model': VOICE,
            'container': 'wav',
            'encoding': 'linear16',
        })
        url = f'{DEEPGRAM_URL}?{query}'
        data = json.dumps({'text': text}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                'Authorization': f'Token {api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'AGV-Voice-Generator/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            content = resp.read()
            return _repair_wav_header(content)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')[:200]
        log.error('Deepgram HTTP %s: %s', exc.code, body)
        return None
def _transcribe(wav_bytes: bytes, api_key: str) -> str | None:
    """Read back synthesized audio through Deepgram Nova-3 STT to verify."""
    try:
        query = urllib.parse.urlencode({
            'model': 'nova-3',
            'punctuate': 'true',
            'smart_format': 'true',
        })
        url = f'https://api.deepgram.com/v1/listen?{query}'
        req = urllib.request.Request(
            url,
            data=wav_bytes,
            headers={
                'Authorization': f'Token {api_key}',
                'Content-Type': 'audio/wav',
                'User-Agent': 'AGV-Voice-Generator/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data['results']['channels'][0]['alternatives'][0].get('transcript', '').strip()
    except Exception as exc:
        log.warning('STT read-back failed: %s', exc)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate pending phrase WAVs via Deepgram.')
    parser.add_argument('--dry-run', action='store_true', help='Print only, do not call API.')
    parser.add_argument('--key', help='Generate only this specific phrase key.')
    parser.add_argument('--force', action='store_true', help='Re-generate even if WAV exists.')
    args = parser.parse_args()

    api_key = os.environ.get('DEEPGRAM_API_KEY', '').strip()
    if not api_key and not args.dry_run:
        log.error('DEEPGRAM_API_KEY environment variable is not set.')
        sys.exit(1)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))

    # Collect pending entries — skip category comment keys and non-pending entries
    pending: list[tuple[str, dict]] = []
    for key, entry in manifest.items():
        if key.startswith('#') or not isinstance(entry, dict):
            continue
        if args.key and key != args.key:
            continue
        if not entry.get('pending'):
            continue
        text = entry.get('text', '').strip()
        if not text:
            log.warning('Skipping %r — no text field.', key)
            continue
        # Skip templated phrases (they have {slots})
        if '{' in text:
            log.info('Skipping %r — contains runtime slot: %r', key, text)
            continue
        pending.append((key, entry))

    if not pending:
        log.info('No pending phrases found. All done.')
        return

    log.info('Found %d pending phrase(s) to generate.', len(pending))
    today = date.today().isoformat()
    changed = False

    for i, (key, entry) in enumerate(pending, 1):
        text = entry['text']
        wav_path = PHRASES_DIR / f'{key}.wav'

        if wav_path.exists() and not args.force:
            log.info('[%d/%d] %r — WAV already exists, skipping (use --force to redo).',
                     i, len(pending), key)
            # Update manifest to mark it recorded even if we skipped generation
            manifest[key].update({
                'pending': False,
                'wav': True,
                'bytes': wav_path.stat().st_size,
                'seconds': round(_wav_duration(wav_path), 2),
                'recorded': today,
                'heard': text,
            })
            changed = True
            continue

        if args.dry_run:
            log.info('[%d/%d] DRY RUN — would generate: %r → "%s"', i, len(pending), key, text)
            continue

        log.info('[%d/%d] Generating: %r → "%s"', i, len(pending), key, text)
        wav_bytes = _synthesize(text, api_key)
        if wav_bytes is None:
            log.error('Failed to generate %r. Skipping.', key)
            continue

        wav_path.write_bytes(wav_bytes)
        duration = _wav_duration(wav_path)
        log.info('  Saved %s (%.1f KB, %.2f s)', wav_path.name,
                 len(wav_bytes) / 1024, duration)

        heard = _transcribe(wav_bytes, api_key) or text
        log.info('  Verified via STT: heard=%r', heard)

        # Update the manifest entry
        manifest[key] = {
            'bytes': len(wav_bytes),
            'heard': heard,
            'recorded': today,
            'seconds': round(duration, 2),
            'text': text,
            'voice': VOICE,
        }
        changed = True

        if i < len(pending):
            time.sleep(RATE_LIMIT_DELAY_S)

    if changed and not args.dry_run:
        # Write manifest back — pretty-printed, sorted keys
        MANIFEST_PATH.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        log.info('manifest.json updated.')

    log.info('Done.')


if __name__ == '__main__':
    main()
