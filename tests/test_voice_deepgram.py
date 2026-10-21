"""Tests for the Deepgram STT and TTS lanes.

No network. The Deepgram backend takes an injectable ``post`` callable
so the request shape and the response parsing can be pinned without a
key or a round-trip.
"""

import _env  # noqa: F401

import contextlib
import io
import os
import struct
import wave

import voice.stt as stt
from voice._constants import MIC_SAMPLE_RATE
from voice.stt import (
    DEEPGRAM_API_KEY_ENV,
    DEEPGRAM_STT_MODEL,
    DeepgramSttBackend,
    SttError,
    _pcm_to_wav,
    _transcript_from_deepgram,
    default_backend,
)
from voice.tts import DeepgramTtsBackend


# ── helpers ─────────────────────────────────────────────────────────


def _pcm(samples=1600):
    return struct.pack('<h', 400) * samples


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b'', text=''):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or (str(payload) if payload else '')

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class FakeRequests:
    """Records calls and replays a canned response."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, params=None, headers=None, data=None, json=None,
             timeout=None):
        self.calls.append({'url': url, 'params': params, 'headers': headers,
                           'data': data, 'json': json, 'timeout': timeout})
        if self.error:
            raise self.error
        return self.response


def _payload(text='go forward two metres', confidence=0.93):
    return {
        'results': {
            'channels': [
                {'alternatives': [{'transcript': text, 'confidence': confidence}]}
            ]
        },
        'metadata': {'duration': 1.2},
    }


@contextlib.contextmanager
def _with_fake_requests(fake):
    """Install a fake ``requests`` module in sys.modules for the duration."""
    import sys
    import types
    module = types.ModuleType('requests')
    module.post = fake.post
    saved = sys.modules.get('requests')
    sys.modules['requests'] = module
    try:
        yield fake
    finally:
        if saved is not None:
            sys.modules['requests'] = saved
        else:
            sys.modules.pop('requests', None)


# ── availability ────────────────────────────────────────────────────


def test_missing_key_leaves_backend_unavailable():
    b = DeepgramSttBackend(api_key='')
    assert b.available is False
    assert 'DEEPGRAM_API_KEY' in b.unavailable_reason


def test_key_is_read_from_env():
    os.environ[DEEPGRAM_API_KEY_ENV] = 'dg_test_key'
    try:
        assert DeepgramSttBackend().available is True
    finally:
        os.environ.pop(DEEPGRAM_API_KEY_ENV, None)


def test_key_is_stripped():
    assert DeepgramSttBackend(api_key='  spaced  ')._api_key == 'spaced'


def test_transcribe_raises_without_key():
    try:
        DeepgramSttBackend(api_key='').transcribe(_pcm())
    except SttError:
        return
    raise AssertionError('expected SttError')


def test_transcribe_rejects_empty_audio():
    try:
        DeepgramSttBackend(api_key='k').transcribe(b'')
    except SttError:
        return
    raise AssertionError('expected SttError')


# ── request shape ───────────────────────────────────────────────────


def test_request_targets_listen_endpoint_with_nova3():
    fake = FakeRequests(FakeResponse(payload=_payload()))
    backend = DeepgramSttBackend(api_key='k')
    with _with_fake_requests(fake):
        backend.transcribe(_pcm())
    call = fake.calls[0]
    assert call['url'].endswith('/v1/listen')
    assert call['params']['model'] == DEEPGRAM_STT_MODEL
    assert call['params']['language'] == 'en'
    assert call['params']['interim_results'] == 'false'


def test_request_sends_token_auth_header():
    fake = FakeRequests(FakeResponse(payload=_payload()))
    with _with_fake_requests(fake):
        DeepgramSttBackend(api_key='secret').transcribe(_pcm())
    auth = fake.calls[0]['headers']['Authorization']
    assert auth == 'Token secret'


def test_request_body_is_a_wav():
    fake = FakeRequests(FakeResponse(payload=_payload()))
    with _with_fake_requests(fake):
        DeepgramSttBackend(api_key='k').transcribe(_pcm())
    body = fake.calls[0]['data']
    assert body[:4] == b'RIFF'
    assert body[8:12] == b'WAVE'


def test_pcm_to_wav_roundtrips_samples():
    pcm = _pcm(800)
    wav = _pcm_to_wav(pcm)
    with wave.open(io.BytesIO(wav), 'rb') as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == MIC_SAMPLE_RATE
        assert w.readframes(w.getnframes()) == pcm


# ── response parsing ────────────────────────────────────────────────


def test_transcribe_returns_transcript():
    fake = FakeRequests(FakeResponse(payload=_payload()))
    with _with_fake_requests(fake):
        t = DeepgramSttBackend(api_key='k').transcribe(_pcm())
    assert t.text == 'go forward two metres'
    assert abs(t.confidence - 0.93) < 0.001
    assert t.source == 'deepgram'


def test_transcribe_strips_whitespace_from_transcript():
    fake = FakeRequests(FakeResponse(
        payload=_payload(text='  stop  ')))
    with _with_fake_requests(fake):
        assert DeepgramSttBackend(api_key='k').transcribe(_pcm()).text == 'stop'


def test_transcribe_raises_on_blank_transcript():
    fake = FakeRequests(FakeResponse(payload=_payload(text='   ')))
    try:
        with _with_fake_requests(fake):
            DeepgramSttBackend(api_key='k').transcribe(_pcm())
    except SttError:
        return
    raise AssertionError('expected SttError on blank transcript')


def test_transcribe_raises_on_http_error():
    fake = FakeRequests(FakeResponse(status_code=401, text='invalid key'))
    try:
        with _with_fake_requests(fake):
            DeepgramSttBackend(api_key='k').transcribe(_pcm())
    except SttError as exc:
        assert '401' in str(exc)
        return
    raise AssertionError('expected SttError on HTTP 401')


def test_transcribe_wraps_network_error():
    fake = FakeRequests(error=RuntimeError('connection reset'))
    try:
        with _with_fake_requests(fake):
            DeepgramSttBackend(api_key='k').transcribe(_pcm())
    except SttError as exc:
        assert 'connection reset' in str(exc)
        return
    raise AssertionError('expected SttError')


def test_malformed_payload_raises_stterror():
    for bad in ({}, {'results': {}}, {'results': {'channels': []}},
                {'results': {'channels': [{'alternatives': []}]}}):
        try:
            _transcript_from_deepgram(bad, 'deepgram', 'en')
        except SttError:
            continue
        raise AssertionError(f'expected SttError for payload {bad}')


def test_missing_confidence_defaults_to_zero():
    payload = {'results': {'channels': [
        {'alternatives': [{'transcript': 'stop'}]}]}}
    t = _transcript_from_deepgram(payload, 'deepgram', 'en')
    assert t.confidence == 0.0


# ── backend selection ───────────────────────────────────────────────


def test_default_backend_prefers_deepgram():
    os.environ[DEEPGRAM_API_KEY_ENV] = 'dg'
    try:
        assert isinstance(default_backend(), DeepgramSttBackend)
    finally:
        os.environ.pop(DEEPGRAM_API_KEY_ENV, None)


def test_default_backend_without_keys_is_deepgram_but_unavailable():
    """Never silently local — an honest 'voice input off' beats a
    model nobody asked for."""
    saved = {k: os.environ.pop(k) for k in
             (DEEPGRAM_API_KEY_ENV, 'GOOGLE_APPLICATION_CREDENTIALS', 'STT_API_KEY')
             if k in os.environ}
    try:
        backend = default_backend()
        assert backend.available is False
    finally:
        os.environ.update(saved)


# ── Deepgram TTS ────────────────────────────────────────────────────


def test_tts_missing_key_is_unavailable():
    backend = DeepgramTtsBackend(api_key='')
    assert backend.available is False
    assert backend.synthesize('hello') is None


def test_tts_requests_wav_not_mp3():
    """aplay is the first playback tool and cannot decode MP3."""
    fake = FakeRequests(FakeResponse(content=b'RIFF....WAVEfmt '))
    with _with_fake_requests(fake):
        audio = DeepgramTtsBackend(api_key='k').synthesize('Moving.')
    params = fake.calls[0]['params']
    assert params['container'] == 'wav'
    assert params['encoding'] == 'linear16'
    assert fake.calls[0]['json'] == {'text': 'Moving.'}
    assert audio.startswith(b'RIFF')


def test_tts_returns_none_on_http_error():
    fake = FakeRequests(FakeResponse(status_code=429, text='rate limited'))
    with _with_fake_requests(fake):
        assert DeepgramTtsBackend(api_key='k').synthesize('hi') is None


def test_tts_returns_none_on_network_error():
    fake = FakeRequests(error=RuntimeError('timeout'))
    with _with_fake_requests(fake):
        assert DeepgramTtsBackend(api_key='k').synthesize('hi') is None


def test_tts_returns_none_on_empty_text():
    fake = FakeRequests(FakeResponse(content=b'RIFF'))
    with _with_fake_requests(fake):
        assert DeepgramTtsBackend(api_key='k').synthesize('   ') is None
    assert fake.calls == []


def test_engine_reports_synth_unavailable_without_key():
    from voice.tts import TtsEngine
    engine = TtsEngine(synthesizer=DeepgramTtsBackend(api_key=''))
    assert engine.synth_available is False


def test_engine_speaks_synthesized_when_key_present():
    """Cache miss + key present must actually play something."""
    import tempfile
    from pathlib import Path
    from voice.tts import PhraseCache, TtsEngine

    played = []

    class FakePlayback:
        def play(self, path):
            played.append(Path(path))
            return True

    class FakeSynth:
        available = True

        def synthesize(self, text):
            return b'RIFF----WAVEfmt fake audio'

    with tempfile.TemporaryDirectory() as d:
        engine = TtsEngine(cache=PhraseCache(directory=Path(d)),
                           playback=FakePlayback(),
                           synthesizer=FakeSynth())
        out = engine.speak_event('lidar_hold', 'Holding — obstacle.')
    assert out.played is True
    assert out.cache_key is None
    assert len(played) == 1
    assert played[0].suffix == '.wav'
    assert not played[0].exists(), 'temp wav must be cleaned up'


def test_engine_stays_silent_when_synthesis_fails():
    import tempfile
    from pathlib import Path
    from voice.tts import PhraseCache, TtsEngine

    class BrokenSynth:
        available = True

        def synthesize(self, text):
            return None

    with tempfile.TemporaryDirectory() as d:
        engine = TtsEngine(cache=PhraseCache(directory=Path(d)),
                           synthesizer=BrokenSynth())
        out = engine.speak_event('lidar_hold', 'Holding.')
    assert out.played is False


if __name__ == '__main__':
    _env.run_tests(globals())