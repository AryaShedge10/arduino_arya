"""Tests for voice.stt — Google Speech-to-Text v2 wiring.

No network, no credentials. The v2 client is a fake; construction with
no credentials is tested for graceful degradation.
"""

import _env  # noqa: F401

import contextlib
import io
import struct
import wave

import voice.stt as stt
from voice._constants import MIC_SAMPLE_RATE
from voice.mic import LevelMeter, MicDevice
from voice.stt import (
    STT_CONFIDENCE_FLOOR,
    GoogleSttBackend,
    OfflineSttBackend,
    SttEngine,
    SttError,
    Transcript,
)


# ── helpers ─────────────────────────────────────────────────────────


@contextlib.contextmanager
def _patched(module, name, value):
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield value
    finally:
        setattr(module, name, original)


def _pcm(samples=16000):
    return struct.pack('<h', 800) * samples


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class _Alt:
    def __init__(self, transcript, confidence=0.9):
        self.transcript = transcript
        self.confidence = confidence


class _Result:
    def __init__(self, alternatives):
        self.alternatives = alternatives


class _Response:
    def __init__(self, results):
        self.results = results


class FakeClient:
    """Stands in for speech_v2 SpeechClient."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def recognize(self, request=None):
        self.calls.append(request)
        if self._error:
            raise self._error
        return self._response


def _backend(client, **kw):
    """GoogleSttBackend wired to a fake client, skipping real creds."""
    b = GoogleSttBackend.__new__(GoogleSttBackend)
    b.model = kw.get('model', 'chirp')
    b.model_version = kw.get('model_version', 'latest_short')
    b.language = kw.get('language', 'en-US')
    b._unavailable_reason = ''
    b._client = (client, _FakeTypes())
    return b


class _FakeTypes:
    """Minimal stand-in matching the shapes stt.py constructs."""

    class RecognitionConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class RecognitionFeatures:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class ExplicitDecodingConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class RecognizeRequest:
        def __init__(self, **kw):
            self.__dict__.update(kw)


# ── transcript shape ────────────────────────────────────────────────


def test_transcript_is_frozen():
    t = Transcript(text='stop', confidence=0.9)
    try:
        t.text = 'go'
    except Exception:
        return
    raise AssertionError('Transcript must be frozen')


def test_transcript_defaults_to_english():
    assert Transcript(text='hi', confidence=0.5).language == 'en-US'


# ── backend: unavailable states ─────────────────────────────────────


def test_missing_credentials_leaves_backend_unavailable():
    """Constructing with no ADC must not raise — it logs and disables."""
    b = GoogleSttBackend()
    if b.available:
        return                      # box has creds; nothing to assert here
    assert 'credential' in b.unavailable_reason.lower()


def test_transcribe_raises_stterror_when_unavailable():
    b = GoogleSttBackend()
    b._client = None
    b._unavailable_reason = 'no creds'
    try:
        b.transcribe(_pcm())
    except SttError:
        return
    raise AssertionError('expected SttError')


def test_transcribe_rejects_empty_audio():
    b = _backend(FakeClient())
    try:
        b.transcribe(b'')
    except SttError:
        return
    raise AssertionError('expected SttError on empty audio')


# ── backend: successful recognition ─────────────────────────────────


def test_transcribe_returns_transcript():
    client = FakeClient(_Response([_Result([_Alt('go forward two metres', 0.92)])]))
    b = _backend(client)
    t = b.transcribe(_pcm())
    assert t.text == 'go forward two metres'
    assert abs(t.confidence - 0.92) < 0.001
    assert t.source == 'google'


def test_transcribe_sets_linear16_and_rate_on_request():
    client = FakeClient(_Response([_Result([_Alt('stop', 0.9)])]))
    b = _backend(client)
    b.transcribe(_pcm(), sample_rate=16000)
    req = client.calls[0]
    assert req.config.explicit_decoding_config.encoding == 'LINEAR16'
    assert req.config.explicit_decoding_config.sample_rate_hertz == 16000
    assert req.config.explicit_decoding_config.audio_channel_count == 1
    assert req.config.model == 'chirp/latest_short'
    assert list(req.config.language_codes) == ['en-US']


def test_transcribe_disables_punctuation():
    """Commands aren't dictation — 'yes' must not come back 'yes.'"""
    client = FakeClient(_Response([_Result([_Alt('yes', 0.9)])]))
    b = _backend(client)
    b.transcribe(_pcm())
    assert client.calls[0].config.features.enable_automatic_punctuation is False


def test_transcribe_skips_empty_alternatives():
    client = FakeClient(_Response([
        _Result([_Alt('   ', 0.9)]),
        _Result([_Alt('move forward', 0.88)]),
    ]))
    assert _backend(client).transcribe(_pcm()).text == 'move forward'


def test_transcribe_raises_when_nothing_recognised():
    client = FakeClient(_Response([]))
    try:
        _backend(client).transcribe(_pcm())
    except SttError:
        return
    raise AssertionError('expected SttError on empty results')


def test_transcribe_wraps_network_failure():
    client = FakeClient(error=RuntimeError('connection reset'))
    try:
        _backend(client).transcribe(_pcm())
    except SttError as exc:
        assert 'connection reset' in str(exc)
        return
    raise AssertionError('expected SttError')


# ── offline stand-in ────────────────────────────────────────────────


def test_offline_backend_returns_canned_text():
    b = OfflineSttBackend(text='go forward two metres')
    assert b.transcribe(_pcm()).text == 'go forward two metres'
    assert b.calls == 1


def test_offline_backend_rejects_empty_audio():
    try:
        OfflineSttBackend().transcribe(b'')
    except SttError:
        return
    raise AssertionError('expected SttError')


# ── engine ──────────────────────────────────────────────────────────


class FakeMic:
    def __init__(self, wav=None):
        self.wav = wav
        self.available = True
        self.calls = 0

    def listen(self, seconds=None, meter=None):
        self.calls += 1
        return self.wav


def test_hear_returns_transcript_when_all_gates_pass():
    mic = FakeMic(_wav(_pcm(16000 * 3)))
    e = SttEngine(backend=OfflineSttBackend(text='stop'),
                  mic=mic, meter=LevelMeter.for_defaults())
    # Offline backend ignores PCM level; gates that matter are confidence.
    t = e.hear()
    assert t is not None
    assert t.text == 'stop'


def test_hear_returns_none_when_mic_silent():
    mic = FakeMic(None)
    e = SttEngine(backend=OfflineSttBackend(), mic=mic)
    assert e.hear() is None


def test_hear_returns_none_when_unavailable():
    class DeadBackend:
        available = False

        def transcribe(self, pcm, sample_rate=16000):
            raise AssertionError('must not be called')

    e = SttEngine(backend=DeadBackend(), mic=FakeMic(_wav(_pcm())))
    assert e.unavailable is True
    assert e.hear() is None


def test_hear_drops_low_confidence():
    e = SttEngine(backend=OfflineSttBackend(text='burn right',
                                           confidence=STT_CONFIDENCE_FLOOR - 0.1),
                  mic=FakeMic(_wav(_pcm())))
    assert e.hear() is None


def test_hear_accepts_at_floor():
    e = SttEngine(backend=OfflineSttBackend(text='turn right',
                                           confidence=STT_CONFIDENCE_FLOOR),
                  mic=FakeMic(_wav(_pcm())))
    assert e.hear() is not None


def test_hear_swallows_backend_error():
    class Angry:
        available = True

        def transcribe(self, pcm, sample_rate=16000):
            raise SttError('quota exceeded')

    e = SttEngine(backend=Angry(), mic=FakeMic(_wav(_pcm())))
    assert e.hear() is None


def test_hear_returns_none_for_wav_without_pcm():
    e = SttEngine(backend=OfflineSttBackend(), mic=FakeMic(b'not a wav'))
    assert e.hear() is None


def test_engine_does_not_raise_without_credentials():
    """The default engine on a credless box must construct and be inert."""
    e = SttEngine()
    assert e.backend is not None
    if not e.backend.available:
        assert e.unavailable is True


if __name__ == '__main__':
    _env.run_tests(globals())