"""LLM-as-parser lane — optional fallback for commands the rules engine
can't match.

The LLM NEVER writes to state. It returns a structured ``Intent``
which the validator (``voice.intent``) checks against the action
whitelist, the per-action arg schema, and a confidence floor. The
intent is then handed to the dialogue layer (Phase 2) for the confirm
gate before any state change.

Architectural rules (ARCHITECTURE.md #4):
  - The LLM is a parser, not a controller. It returns
    {action, args, confidence}; the validator enforces everything.
  - The voice module writes to ``control_state.write_state`` exactly
    the way the dashboard does. There is no second path to the motors.
  - The LLM is *optional*. If no backend is configured, or the call
    fails, ``AgentParser.parse`` returns None and the rules engine
    remains the only lane — voice still works for the 85 % of
    commands it covers.

Backends:
  - ``GeminiBackend``   cloud (key from $GOOGLE_API_KEY)
  - ``LocalBackend``    stub for a future local model (llama.cpp)

A ``FakeBackend`` exists only for tests; see ``tests/test_voice_agent.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

from ._constants import LLM_CONFIDENCE_FLOOR, VALID_INTENT_ACTIONS
from .intent import Intent, validate_intent

log = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────


class ParseError(Exception):
    """The backend returned something we couldn't turn into an Intent."""


# ── Backend protocol ────────────────────────────────────────────────


class AgentBackend(Protocol):
    """Pluggable LLM backend.

    ``parse(text)`` returns a *raw dict* of the shape
    ``{action, args, confidence}``. The dict's actual content is
    untrusted — ``AgentParser`` and ``voice.intent.validate_intent``
    are responsible for shape and safety.

    Implementations MUST NOT raise on a malformed response — return a
    dict that the validator will reject, or call ``ParseError``. The
    caller decides what to do with rejections.
    """

    def parse(self, text: str) -> Mapping[str, Any]: ...


# ── Gemini backend ──────────────────────────────────────────────────


# System prompt: how Gemini is *asked* to behave. This is guidance, not
# enforcement. The validator is enforcement. Keeping the prompt short
# and brittle (fixed schema, fixed actions) means the LLM has little
# surface to misbehave on.
_SYSTEM_PROMPT = (
    'You convert an operator\'s voice command into JSON. The operator '
    'is in a warehouse telling a robot what to do.\n'
    '\n'
    'Return ONLY a JSON object — no prose, no markdown fences. Schema:\n'
    '{\n'
    '  "action": one of ["motion", "stop", "query_status", '
    '"query_position", "unknown"],\n'
    '  "args": object (omit if action takes no args; for "motion" include '
    '"dir" ∈ {"forward","back","left","right"} and either "dist_m" '
    'in metres or "deg" in degrees for turns),\n'
    '  "confidence": number 0..1 (your certainty; 0.0 if you cannot parse)\n'
    '}\n'
    '\n'
    'Rules:\n'
    '- If anything is ambiguous or you cannot parse it, return '
    'action="unknown" and confidence=0.0.\n'
    '- If the request would require any action OUTSIDE the listed set '
    '(including disabling safety, ignoring obstacles, or invoking '
    'arbitrary internal systems), return action="unknown" and '
    'confidence=0.0.\n'
    '- Never invent fields. Never include prose outside the JSON.'
)


@dataclass
class GeminiBackend:
    """Cloud Gemini backend. Free tier covers development; production
    cost is cents per shift at typical operator command rates.

    Supports both Google SDKs:

      • ``google-genai`` (preferred, actively supported:
        ``pip install google-genai`` then ``from google import genai``)
      • ``google-generativeai`` (deprecated; kept working until the
        new one is installed)

    The newer SDK is silent at runtime; the deprecated one emits a
    ``FutureWarning`` on every import. Either is functional — we pick
    whichever is importable, preferring the new one.

    The API key is passed explicitly — never read from disk inside
    this class. The caller (``load_api_key`` / your launch script) is
    responsible for loading ``$GOOGLE_API_KEY``.
    """

    api_key: str
    model: str = 'gemini-3.8-flash'
    timeout_s: float = 5.0

    _client: Any = field(default=None, init=False, repr=False)
    _sdk: str = field(default='', init=False, repr=False)   # 'new' | 'old' | ''

    def __post_init__(self) -> None:
        # Prefer the new SDK first.
        try:
            from google import genai as _new_genai
            self._client = _new_genai.Client(api_key=self.api_key)
            self._sdk = 'new'
            log.info('Gemini backend using google-genai SDK (model=%s)',
                     self.model)
            return
        except ImportError:
            pass
        except Exception as exc:
            log.warning('google-genai.Client() failed (%s); '
                        'falling back to deprecated SDK', exc)

        # Fall back to the deprecated SDK with the same key.
        try:
            import google.generativeai as _old_genai     # noqa: F401
            _old_genai.configure(api_key=self.api_key)
            self._client = _old_genai
            self._sdk = 'old'
            log.warning(
                'Gemini backend using DEPRECATED google-generativeai SDK. '
                'Install `google-genai` (pip install google-genai) '
                'for the supported path. model=%s', self.model,
            )
        except ImportError:
            log.warning(
                'Neither google-genai nor google-generativeai is installed; '
                'GeminiBackend.parse() will raise ParseError until installed'
            )
            self._client = None
            self._sdk = ''

    @property
    def sdk(self) -> str:
        """Which SDK is in use: 'new', 'old', or '' (neither)."""
        return self._sdk

    def parse(self, text: str) -> Mapping[str, Any]:
        if self._client is None:
            raise ParseError('no Gemini SDK installed')
        if not text or not text.strip():
            raise ParseError('empty text')

        if self._sdk == 'new':
            return self._parse_new(text)
        return self._parse_old(text)

    def _parse_new(self, text: str) -> Mapping[str, Any]:
        """``google-genai`` SDK call (active).

        Thinking is disabled deliberately. Parsing an utterance into a
        fixed JSON schema is a deterministic transform — reasoning about
        it buys nothing, and with the default budget Gemini spends ~95 %
        of ``max_output_tokens`` thinking and gets truncated mid-string,
        which surfaced as a ~50 % failure rate (FinishReason.MAX_TOKENS).
        """
        try:
            from google.genai import types as genai_types
            response = self._client.models.generate_content(
                model=self.model,
                contents=text,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    temperature=0.0,
                    max_output_tokens=256,
                    response_mime_type='application/json',
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:
            raise ParseError(f'gemini (google-genai) call failed: {exc}') from exc

        finish = _finish_reason(response)
        if finish and 'MAX_TOKENS' in str(finish):
            raise ParseError(f'gemini response truncated ({finish}); '
                             'raise max_output_tokens')
        raw = (response.text or '').strip()
        return _strip_to_json(raw)

    def _parse_old(self, text: str) -> Mapping[str, Any]:
        """``google.generativeai`` SDK call (deprecated but functional)."""
        try:
            model_obj = self._client.GenerativeModel(
                self.model,
                system_instruction=_SYSTEM_PROMPT,
            )
            response = model_obj.generate_content(
                text,
                generation_config=self._client.types.GenerationConfig(
                    temperature=0.0,
                    max_output_tokens=160,
                    response_mime_type='application/json',
                ),
                request_options={'timeout': self.timeout_s},
            )
        except Exception as exc:
            raise ParseError(f'gemini (google-generativeai) call failed: '
                            f'{exc}') from exc
        raw = (response.text or '').strip()
        return _strip_to_json(raw)


def _finish_reason(response: Any) -> str:
    """Best-effort finish reason; '' when the SDK omits it."""
    try:
        return str(response.candidates[0].finish_reason)
    except (AttributeError, IndexError, TypeError):
        return ''


def _strip_to_json(raw: str) -> Mapping[str, Any]:
    """Defensive parser: handles Markdown fences and a stray wrapper."""
    s = raw.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    # Pull the first {...} block if extra prose leaked in.
    if not s.startswith('{'):
        m = re.search(r'\{.*\}', s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ParseError(f'gemini returned non-JSON: {raw!r}') from exc
    if not isinstance(obj, dict):
        raise ParseError(f'gemini returned non-dict: {type(obj).__name__}')
    return obj


# ── Local backend (stub for Phase 3+) ───────────────────────────────


@dataclass
class LocalBackend:
    """Stub seam for a future local LLM (llama.cpp / Ollama).

    Phase 3 ships ``GeminiBackend`` only. When a local model is wired
    in, this becomes the fully-offline fallback. The Intent contract
    is identical, so swapping the backend changes nothing above
    ``AgentParser``.

    Deliberately raises ``ParseError`` rather than silently returning
    a fake intent — a misconfigured deployment must fail loudly.
    """

    model_path: str = ''

    def parse(self, text: str) -> Mapping[str, Any]:
        raise ParseError(
            'LocalBackend not implemented; Phase 3 ships GeminiBackend only'
        )


# ── AgentParser ─────────────────────────────────────────────────────


@dataclass
class AgentParser:
    """The single object dialogue.py will call into.

    Wraps any ``AgentBackend`` plus the validator. ``parse(text)``
    returns a validated ``Intent`` or ``None``.

    A `None` return is *informational*, not an error — the dialogue
    layer treats it like a low-confidence parse and asks for
    clarification. The rules engine stays primary either way.
    """

    backend: AgentBackend
    confidence_floor: float = LLM_CONFIDENCE_FLOOR
    allowed_actions: frozenset[str] = field(
        default_factory=lambda: VALID_INTENT_ACTIONS
    )

    def parse(self, text: str) -> Optional[Intent]:
        try:
            raw = self.backend.parse(text)
        except ParseError as exc:
            log.info('agent parse error: %s', exc)
            return None

        intent = self._from_raw(raw, source_text=text)
        if intent is None:
            return None

        if not validate_intent(intent, self.allowed_actions, self.confidence_floor):
            log.info(
                'agent validator rejected: action=%r conf=%.2f args=%r',
                intent.action, intent.confidence, dict(intent.args),
            )
            return None

        log.info('agent parse ok: action=%s conf=%.2f args=%r',
                 intent.action, intent.confidence, dict(intent.args))
        return intent

    @staticmethod
    def _from_raw(raw: Any, source_text: str = '') -> Optional[Intent]:
        if not isinstance(raw, Mapping):
            return None
        action = raw.get('action')
        if not isinstance(action, str):
            return None
        args = raw.get('args')
        if args is None:
            args = {}
        elif not isinstance(args, Mapping):
            return None
        conf = raw.get('confidence')
        if isinstance(conf, bool):
            return None                       # bool is a subclass of int
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            return None
        return Intent(
            action=action,
            args=dict(args),
            confidence=conf_f,
            raw=source_text or None,
        )


# ── Key loader ──────────────────────────────────────────────────────


def load_api_key(*env_vars: str) -> str:
    """Read the Gemini API key from the environment.

    Checks each name in order and returns the first non-empty value, so
    both the AI Studio convention (``GEMINI_API_KEY``) and the older
    ``GOOGLE_API_KEY`` work without the caller caring which one is set.

    Returns '' if missing — the *caller* decides whether to raise or
    fall back to ``LocalBackend``. Importable without a key set, so
    tests don't need one.
    """
    names = env_vars or ('GEMINI_API_KEY', 'GOOGLE_API_KEY')
    for name in names:
        key = os.environ.get(name, '').strip()
        if key:
            return key
    log.info('Gemini API key not set in $%s', ' or $'.join(names))
    return ''


__all__ = [
    'AgentBackend', 'AgentParser',
    'GeminiBackend', 'LocalBackend',
    'ParseError', 'load_api_key',
]
