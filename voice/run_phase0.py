"""Phase 0 entry point: TTS-only, no mic, no control writes.

Reads the main project's control.json + points.json and plays (or logs)
Tarka's phrases in response to transitions. Run this *alongside* the
AGV / dashboard; you'll hear Tarka narrate enable transitions, mode
changes, password-gated overrides, and LiDAR holds.

This module does NOT touch the main project. It polls read-only JSON
files the project writes anyway. Stop with Ctrl-C.

Usage::

    python -m voice.run_phase0               # use AGV_DASHBOARD_DIR / default
    python -m voice.run_phase0 --dry-run     # log only, no audio
    python -m voice.run_phase0 --poll 0.5    # 2 Hz polling
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from config import TELEMETRY_DIR

from voice.event_watcher import EventWatcher
from voice.tts import SilentTts, TtsEngine


log = logging.getLogger(__name__)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Voice phase 0 runner.')
    ap.add_argument(
        '--telemetry-dir',
        default=os.environ.get('AGV_DASHBOARD_DIR') or TELEMETRY_DIR,
        help='Directory containing control.json + points.json '
             '(default: $AGV_DASHBOARD_DIR or %s)' % TELEMETRY_DIR,
    )
    ap.add_argument(
        '--poll', type=float, default=0.2,
        help='Poll interval in seconds (default: 0.2)',
    )
    ap.add_argument(
        '--dry-run', action='store_true',
        help="Don't play audio; log utterances only.",
    )
    ap.add_argument(
        '-v', '--verbose', action='store_true',
        help='Enable debug-level logging.',
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.dry_run:
        speaker = SilentTts()
        log.info('dry-run mode: utterances logged, no audio playback')
    else:
        speaker = TtsEngine()

    watcher = EventWatcher(
        telemetry_dir=args.telemetry_dir,
        speaker=speaker,
        poll_interval_s=args.poll,
    )
    log.info('voice phase 0 watcher starting (telemetry_dir=%s)',
             args.telemetry_dir)
    watcher.run_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
