"""Speech-to-text — Deepgram (primary) or Google Cloud Speech-to-Text v2.

Wired straight to the C270 mic: ``SttEngine().hear()`` records, gates on
the level meter, uploads, and returns a ``Transcript``.

Why cloud
---------
Warehouse audio is the hard case — fans, forklifts, a hard hat on the
operator. Cloud models handle it far better than anything that fits in
this box's ~2.8 GiB of free RAM. The trade is explicit and documented
rather than hidden: **these lanes need the network.** The offline
``OfflineSttBackend`` exists for tests, not production.

Credentials
-----------
Either is picked up from the environment; neither requires a credit card
to start.

  ``DEEPGRAM_API_KEY``
      Deepgram. $200 credit on signup, no card required. Nova-3 is the
      strongest model here and the recommended default.

  ``GOOGLE_APPLICATION_CREDENTIALS`` / ``STT_API_KEY``
      Google Speech-to-Text v2 (Chirp). Requires billing enabled on the
      project — the 60 min/month tier still needs a card on file.

``available`` is False when no credential resolves, so a misconfigured
deployment degrades to "voice input off" rather than raising in the
middle of a shift.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ._constants import MIC_CHANNELS, MIC_SAMPLE_RATE
from .mic import LevelMeter, MicCapture, read_wav_pcm

log = logging.getLogger(__name__)

# Chirp is the current v2 model family. ``latest_short`` is the variant
# tuned for utterances under a minute — every AGV command is far shorter,
# and it is both faster and cheaper than the long-form model.
STT_MODEL = 'chirp'
STT_MODEL_VERSION = 'latest_short'
STT_LANGUAGE = 'en-US'

# ── Deepgram (default lane) ─────────────────────────────────────────
# Nova-3 is Deepgram's most accurate model and needs no credit card to
# start ($200 credit on signup). Same key drives TTS via voice.tts.
DEEPGRAM_STT_URL = 'https://api.deepgram.com/v1/listen'
DEEPGRAM_STT_MODEL = 'nova-3'
DEEPGRAM_STT_LANGUAGE = 'en'
DEEPGRAM_API_KEY_ENV = 'DEEPGRAM_API_KEY'
# Generous: a cloud round-trip on a slow link still beats a misheard
# command. The floor, not the timeout, is what protects the motors.
DEEPGRAM_TIMEOUT_S = 10.0

# Commands this short produce a low-confidence Chirp result more often
# than not, and a wrong short transcript is worse than none — "turn
# right" misheard as "burn right" would parse into a real intent. Below
# this, ``hear()`` returns None and Tarka says "Sorry, I didn't catch
# that."
STT_CONFIDENCE_FLOOR = 0.55

STT_API_KEY_ENV = 'STT_API_KEY'


# ── Result type ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Transcript:
    """One recognised utterance.

    ``confidence`` is Chirp's own 0..1 score. ``source`` records whether
    this came from the cloud or the offline stub, which the event log
    wants when an operator reports "it misheard me" three weeks later.
    """
    text: str
    confidence: float
    language: str = STT_LANGUAGE
    source: str = 'google'
    raw: Optional[object] = field(default=None, repr=False)


class SttError(Exception):
    """The backend could not produce a transcript."""


# ── Backend protocol ────────────────────────────────────────────────


class SttBackend(Protocol):
    """Pluggable transcriber.

    Implementations raise ``SttError`` on any failure; ``SttEngine``
    catches it and degrades to None. They never raise anything else.
    """

    def transcribe(self, pcm: bytes, sample_rate: int = MIC_SAMPLE_RATE) -> Transcript: ...


# ── Google backend ──────────────────────────────────────────────────


class GoogleSttBackend:
    """Google Cloud Speech-to-Text v2 via ``google-cloud-speech``.

    Construction never raises: if the SDK is missing or no credential
    resolves, ``available`` is False and ``transcribe`` raises
    ``SttError``. That keeps an unconfigured box silent instead of
    exploding at the first command.
    """

    def __init__(self,
                 model: str = STT_MODEL,
                 model_version: str = STT_MODEL_VERSION,
                 language: str = STT_LANGUAGE,
                 api_key_env: str = STT_API_KEY_ENV) -> None:
        self.model = model
        self.model_version = model_version
        self.language = language
        self._client = None
        self._unavailable_reason = ''

        try:
            self._client = self._build_client(api_key_env)
        except ImportError as exc:
            self._unavailable_reason = (
                'google-cloud-speech not installed (pip install google-cloud-speech)')
            log.warning(self._unavailable_reason)
        except Exception as exc:
            self._unavailable_reason = f'credential resolution failed: {exc}'
            log.warning('Google STT unavailable: %s', exc)

    @staticmethod
    def _build_client(api_key_env: str):
        from google.cloud.speech_v2 import types
        from google.cloud.speech_v2.services.speech import SpeechClient
        from google.api_core.client_options import ClientOptions

        api_key = os.environ.get(api_key_env, '').strip()
        options = None
        if api_key:
            # A Cloud project key. Not a Gemini/AI Studio key — that
            # one will not authenticate Speech-to-Text.
            options = ClientOptions(api_key=api_key)
            log.info('Google STT using API key from $%s', api_key_env)
        else:
            log.info('Google STT using application default credentials')

        return SpeechClient(client_options=options), types

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def transcribe(self, pcm: bytes, sample_rate: int = MIC_SAMPLE_RATE) -> Transcript:
        if self._client is None:
            raise SttError(self._unavailable_reason or 'Google STT unavailable')
        if not pcm:
            raise SttError('empty audio')

        client, types = self._client
        request = types.RecognizeRequest(
            config=types.RecognitionConfig(
                model=f'{self.model}/{self.model_version}',
                language_codes=[self.language],
                features=types.RecognitionFeatures(
                    enable_word_confidence=True,
                    enable_automatic_punctuation=False,   # commands, not dictation
                    max_alternatives=1,
                ),
                explicit_decoding_config=types.ExplicitDecodingConfig(
                    encoding='LINEAR16',
                    sample_rate_hertz=sample_rate,
                    audio_channel_count=MIC_CHANNELS,
                ),
            ),
            content=pcm,
        )
        try:
            response = client.recognize(request=request)
        except Exception as exc:                # network, quota, auth — all fatal here
            raise SttError(f'speech-to-text call failed: {exc}') from exc

        results = getattr(response, 'results', None) or []
        for result in results:
            alternatives = getattr(result, 'alternatives', None) or []
            if not alternatives:
                continue
            best = alternatives[0]
            text = (getattr(best, 'transcript', '') or '').strip()
            if not text:
                continue
            confidence = float(getattr(best, 'confidence', 0.0) or 0.0)
            return Transcript(text=text, confidence=confidence,
                              language=self.language, source='google',
                              raw=response)
        raise SttError('no speech recognised in audio')


# ── Deepgram backend ────────────────────────────────────────────────


def _pcm_to_wav(pcm: bytes, sample_rate: int = MIC_SAMPLE_RATE,
                channels: int = MIC_CHANNELS) -> bytes:
    """Wrap raw S16_LE samples in a RIFF header.

    Deepgram infers format from the container, so sending the WAV the
    mic already produced avoids a resample on our side.
    """
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class DeepgramSttBackend:
    """Deepgram speech-to-text over plain REST.

    Nova-3 handles warehouse noise better than anything that fits in
    this machine's free RAM, and the free tier needs no credit card —
    which is why this is the default lane over Google.

    Uses the ``requests`` library that is already installed; no new
    dependency. Construction never raises: a missing key leaves
    ``available`` False and ``transcribe`` raises ``SttError``.
    """

    def __init__(self,
                 api_key: Optional[str] = None,
                 model: str = DEEPGRAM_STT_MODEL,
                 language: str = DEEPGRAM_STT_LANGUAGE,
                 url: str = DEEPGRAM_STT_URL,
                 timeout_s: float = DEEPGRAM_TIMEOUT_S,
                 api_key_env: str = DEEPGRAM_API_KEY_ENV) -> None:
        self.model = model
        self.language = language
        self.url = url
        self.timeout_s = timeout_s
        key = api_key if api_key is not None else os.environ.get(api_key_env, '')
        self._api_key = (key or '').strip()
        self._unavailable_reason = ''
        if not self._api_key:
            self._unavailable_reason = (
                f'no API key — set ${api_key_env}')
            log.warning('Deepgram STT unavailable: %s', self._unavailable_reason)

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def _post(self, wav_bytes: bytes):
        import requests
        response = requests.post(
            self.url,
            params={
                'model': self.model,
                'language': self.language,
                'punctuate': 'true',
                'smart_format': 'true',
                'interim_results': 'false',
            },
            headers={
                'Authorization': f'Token {self._api_key}',
                'Content-Type': 'audio/wav',
            },
            data=wav_bytes,
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise SttError(f'deepgram HTTP {response.status_code}: '
                           f'{response.text[:200]}')
        return response.json()

    def transcribe(self, pcm: bytes, sample_rate: int = MIC_SAMPLE_RATE) -> Transcript:
        if not self._api_key:
            raise SttError(self._unavailable_reason or 'no Deepgram API key')
        if not pcm:
            raise SttError('empty audio')

        try:
            payload = self._post(_pcm_to_wav(pcm, sample_rate))
        except SttError:
            raise
        except Exception as exc:
            raise SttError(f'deepgram call failed: {exc}') from exc

        return _transcript_from_deepgram(payload, source='deepgram',
                                         language=self.language)


def _transcript_from_deepgram(payload, source: str, language: str) -> Transcript:
    """Pull the best alternative out of a Deepgram response."""
    try:
        channels = payload['results']['channels']
        alternative = channels[0]['alternatives'][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise SttError(f'unexpected deepgram payload: {exc}') from exc

    text = (alternative.get('transcript') or '').strip()
    if not text:
        raise SttError('no speech recognised in audio')
    return Transcript(text=text,
                      confidence=float(alternative.get('confidence', 0.0) or 0.0),
                      language=language, source=source, raw=payload)


# ── Backend selection ───────────────────────────────────────────────


def default_backend() -> SttBackend:
    """Pick a backend from the environment, best-first.

    Deepgram wins when present: stronger model, no credit card. Google
    is the fallback for an operator who already has Cloud billing.
    Local/offline is never auto-selected — a silent robot is worse than
    an honest "voice input off".
    """
    if os.environ.get(DEEPGRAM_API_KEY_ENV, '').strip():
        return DeepgramSttBackend()
    if (os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
            or os.environ.get(STT_API_KEY_ENV, '').strip()):
        return GoogleSttBackend()
    return DeepgramSttBackend()      # reports unavailable, explains why


# ── Offline stand-in ────────────────────────────────────────────────


class OfflineSttBackend:
    """Does not transcribe. Returns a fixed transcript.

    Present so the dialogue layer can be exercised end-to-end on a bench
    with no credentials and no network. ``transcribe`` raising would be
    honest; returning canned text is what lets tests drive a full
    confirm → execute cycle without touching Google. Never used in
    production — ``SttEngine`` only builds it when explicitly asked.
    """

    def __init__(self, text: str = 'go forward two metres', confidence: float = 0.99):
        self._text = text
        self._confidence = confidence
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def transcribe(self, pcm: bytes, sample_rate: int = MIC_SAMPLE_RATE) -> Transcript:
        self.calls += 1
        if not pcm:
            raise SttError('empty audio')
        return Transcript(text=self._text, confidence=self._confidence,
                          source='offline')


# ── Facade ──────────────────────────────────────────────────────────


@dataclass
class SttEngine:
    """Mic + backend + confidence floor. The object dialogue.py talks to.

    ``hear()`` is the whole incoming path: record from the C270, drop it
    if nobody spoke, transcribe, drop it if the model wasn't sure.

    ``unavailable`` is True when either the mic or the credentials are
    missing — the caller logs it once at startup and carries on with
    the rest of the rover.
    """

    backend: Optional[SttBackend] = None
    mic: Optional[MicCapture] = None
    confidence_floor: float = STT_CONFIDENCE_FLOOR
    listen_seconds: float = 3.0
    meter: Optional[LevelMeter] = None

    def __post_init__(self) -> None:
        if self.mic is None:
            self.mic = MicCapture()
        if self.backend is None:
            self.backend = default_backend()
        if self.meter is None:
            self.meter = LevelMeter.for_defaults()

    @property
    def unavailable(self) -> bool:
        backend_ok = getattr(self.backend, 'available', True)
        mic_ok = self.mic is not None and self.mic.available
        return not (backend_ok and mic_ok)

    def hear(self, seconds: Optional[float] = None) -> Optional[Transcript]:
        """Record, gate, transcribe, gate again. None means "say it again"."""
        if self.unavailable:
            return None

        wav = self.mic.listen(seconds if seconds is not None else self.listen_seconds,
                              meter=self.meter)
        if wav is None:
            return None

        pcm = read_wav_pcm(wav)
        if not pcm:
            log.warning('captured audio had no PCM payload')
            return None

        try:
            transcript = self.backend.transcribe(pcm)
        except SttError as exc:
            log.warning('stt failed: %s', exc)
            return None

        if transcript.confidence < self.confidence_floor:
            log.info('stt below floor: %r conf=%.2f < %.2f',
                     transcript.text, transcript.confidence,
                     self.confidence_floor)
            return None

        log.info('heard %r (%.2f, %s)', transcript.text,
                 transcript.confidence, transcript.source)
        return transcript


__all__ = [
    'Transcript',
    'SttBackend',
    'SttError',
    'SttEngine',
    'GoogleSttBackend',
    'OfflineSttBackend',
    'STT_CONFIDENCE_FLOOR',
    'STT_LANGUAGE',
    'STT_MODEL',
    'STT_MODEL_VERSION',
]