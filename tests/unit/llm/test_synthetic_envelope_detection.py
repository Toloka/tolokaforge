"""Detect & retry on OpenRouter synthetic-error envelopes.

When an upstream provider produces an unrecoverable response (e.g. Gemini
``MALFORMED_FUNCTION_CALL``, ``TOO_MANY_TOOL_CALLS``, or generic OpenRouter
upstream errors), the wire response that reaches litellm has the shape of
a normal completion:

* ``finish_reason`` is post-mapped to ``"stop"``.
* ``message.content`` is a canned filler (empirically ``"I'll help you
  with that."`` for the OpenRouter Gemini route).
* ``message.tool_calls`` may be populated with partial / fabricated
  arguments — the model never produced them coherently.
* ``usage`` reports ``0`` prompt + ``0`` completion tokens.
* The original failure is preserved in
  ``choice.provider_specific_fields["native_finish_reason"]`` (because
  litellm's :class:`Choices` constructor stores the unmapped name there
  whenever the original ``finish_reason`` differed from the post-map
  value).

The bug fixed by these tests: prior to detection, the harness consumed
the synthetic envelope as a legitimate turn — silently corrupting
trajectories and (because the placeholder ``redacted_thinking`` block is
echoed back by :class:`GeminiReasoningCodec.encode_for_replay`) poisoning
every subsequent turn.

Empirical scope (from ``output/collected_new/ots_19_airlines/``,
2026-04-29 evaluation):

* gemini_31_pro: 320/550 trials carry the placeholder UUID (58.2%);
  pass-rate gap vs. trials without it is 14.3pp.
* gemini_30_flash: 192/550 trials carry it (34.9%); gap is 3.9pp.

Detection contract: any non-empty
``provider_specific_fields["native_finish_reason"]`` value in the
"upstream-failed" set causes :meth:`LLMClient.generate` to raise
:class:`RuntimeError`. Tenacity's ``@retry`` decorator on ``generate``
re-attempts; if every attempt is synthetic the error surfaces to the
caller honestly rather than being absorbed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import LLMClient, _detect_synthetic_envelope
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _synthetic_envelope_response(
    native_finish_reason: str,
    *,
    content: str = "I'll help you with that.",
    include_tool_call: bool = False,
) -> MagicMock:
    """Build a litellm-shape response mimicking the OpenRouter synthetic-error envelope.

    Mirrors the post-:class:`litellm.types.utils.Choices` shape: ``finish_reason``
    is ``"stop"`` (post-map) and ``provider_specific_fields["native_finish_reason"]``
    carries the original unmapped name.
    """
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = content
    message.thinking_blocks = None
    message.reasoning_content = None
    message.provider_specific_fields = None
    if include_tool_call:
        tc = MagicMock()
        tc.id = "tool_synthetic_1"
        tc.function = MagicMock()
        tc.function.name = "search"
        tc.function.arguments = '{"query": "x"}'
        message.tool_calls = [tc]
    else:
        message.tool_calls = None

    choice.message = message
    choice.finish_reason = "stop"  # litellm-mapped value
    choice.provider_specific_fields = {"native_finish_reason": native_finish_reason}
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _normal_response(content: str = "Hello.") -> MagicMock:
    """A clean response — no synthetic envelope markers."""
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = content
    message.tool_calls = None
    message.thinking_blocks = None
    message.reasoning_content = None
    message.provider_specific_fields = None
    choice.message = message
    choice.finish_reason = "stop"
    choice.provider_specific_fields = None  # cleanly absent
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=42,
        completion_tokens=10,
        total_tokens=52,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _make_client(
    monkeypatch: pytest.MonkeyPatch, name: str = "google/gemini-3.1-pro-preview"
) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-synth-envelope")
    return LLMClient(ModelConfig(provider="openrouter", name=name))


# ---------------------------------------------------------------------------
# Pure-function detection contract
# ---------------------------------------------------------------------------


class TestDetectSyntheticEnvelope:
    """``_detect_synthetic_envelope`` returns the offending native reason or ``None``."""

    @pytest.mark.parametrize(
        "native",
        [
            "MALFORMED_FUNCTION_CALL",  # Gemini direct
            "MALFORMED_RESPONSE",  # Gemini direct
            "TOO_MANY_TOOL_CALLS",  # Gemini direct
            "FINISH_REASON_UNSPECIFIED",  # Gemini direct
            "error",  # OpenRouter generic wrapper
            "ERROR",  # Cohere
            "network_error",  # Zhipu
        ],
    )
    def test_returns_native_reason_for_known_synthetic_envelopes(self, native: str) -> None:
        response = _synthetic_envelope_response(native)
        assert _detect_synthetic_envelope(response) == native

    def test_returns_none_for_clean_response(self) -> None:
        assert _detect_synthetic_envelope(_normal_response()) is None

    def test_returns_none_when_native_reason_is_benign(self) -> None:
        """``stop_sequence`` / ``end_turn`` are normal Anthropic terminations."""
        response = _synthetic_envelope_response("stop_sequence")
        assert _detect_synthetic_envelope(response) is None

    def test_returns_none_when_provider_specific_fields_absent(self) -> None:
        response = _normal_response()
        response.choices[0].provider_specific_fields = None
        assert _detect_synthetic_envelope(response) is None

    def test_returns_none_when_provider_specific_fields_not_dict(self) -> None:
        """Defensive — some adapters surface PSF as an object; treat as benign."""
        response = _normal_response()
        response.choices[0].provider_specific_fields = "not-a-dict"
        assert _detect_synthetic_envelope(response) is None

    def test_returns_none_on_malformed_response_object(self) -> None:
        """``response.choices`` access errors must not raise — degrade silently."""
        broken = MagicMock()
        broken.choices = []
        assert _detect_synthetic_envelope(broken) is None


# ---------------------------------------------------------------------------
# generate() integrates the detection + raise + retry
# ---------------------------------------------------------------------------


def _disable_tenacity_sleep(client: LLMClient) -> None:
    """Make :meth:`LLMClient.generate`'s ``@retry`` instant for tests.

    Tenacity's exponential backoff would push a 5-attempt failure to ~60s
    of real wall time. Replace the ``Retrying.sleep`` bound on the
    decorator's instance with a no-op.
    """
    # ``client.generate`` is a bound method; the underlying function carries
    # the tenacity ``Retrying`` as the ``.retry`` attribute.
    client.generate.retry.sleep = lambda *args, **kwargs: None  # type: ignore[attr-defined]


class TestGenerateRaisesAndRetries:
    """End-to-end: synthetic envelope triggers retry; clean response is accepted."""

    def test_persistent_synthetic_envelope_raises_after_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Five attempts of MALFORMED_FUNCTION_CALL → ``RuntimeError``."""
        client = _make_client(monkeypatch)
        _disable_tenacity_sleep(client)

        bad = _synthetic_envelope_response("MALFORMED_FUNCTION_CALL")
        with patch("tolokaforge.core.llm.client.completion", return_value=bad) as mock_completion:
            with pytest.raises(RuntimeError, match="synthetic.*MALFORMED_FUNCTION_CALL"):
                client.generate(
                    system="s",
                    messages=[Message(role=MessageRole.USER, content="hi")],
                )
        # 5 attempts × 1 underlying call each = 5 total calls.
        assert mock_completion.call_count == 5

    def test_recovers_when_retry_returns_valid_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two synthetic envelopes followed by a valid response → success."""
        client = _make_client(monkeypatch)
        _disable_tenacity_sleep(client)

        bad1 = _synthetic_envelope_response("MALFORMED_FUNCTION_CALL")
        bad2 = _synthetic_envelope_response("error")
        good = _normal_response("Recovered!")

        with patch(
            "tolokaforge.core.llm.client.completion",
            side_effect=[bad1, bad2, good],
        ) as mock_completion:
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(
                    system="s",
                    messages=[Message(role=MessageRole.USER, content="hi")],
                )

        assert mock_completion.call_count == 3
        assert result.text == "Recovered!"

    def test_clean_response_passes_through_without_extra_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No false positives on the clean path."""
        client = _make_client(monkeypatch)
        _disable_tenacity_sleep(client)

        good = _normal_response("ok")
        with patch("tolokaforge.core.llm.client.completion", return_value=good) as mock_completion:
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(
                    system="s",
                    messages=[Message(role=MessageRole.USER, content="hi")],
                )

        assert mock_completion.call_count == 1
        assert result.text == "ok"


# ---------------------------------------------------------------------------
# The real-world canary: the placeholder UUID never reaches GenerationResult
# ---------------------------------------------------------------------------


# UUID that OpenRouter returns inside ``reasoning.encrypted`` blocks for
# every Gemini MALFORMED_FUNCTION_CALL synthetic envelope.
# Base64-decodes to ``e24830a7-5cd6-42fe-998b-ee539e72b9c3``.
_OPENROUTER_PLACEHOLDER_UUID_B64 = "ZTI0ODMwYTctNWNkNi00MmZlLTk5OGItZWU1MzllNzJiOWMz"


def test_gemini_placeholder_uuid_never_reaches_caller_after_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concrete eval-data regression check.

    Until 2026-04-29 the placeholder UUID
    ``e24830a7-5cd6-42fe-998b-ee539e72b9c3`` (base64
    ``ZTI0ODMwYTctNWNkNi00MmZlLTk5OGItZWU1MzllNzJiOWMz``) appeared in
    320 of 550 Gemini 3.1 Pro trial trajectories — the OpenRouter
    synthetic-envelope marker for upstream MALFORMED_FUNCTION_CALL.
    Now that the synthetic envelope raises, no GenerationResult should
    ever carry this fingerprint.
    """
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)

    # Build a synthetic envelope that ALSO carries the placeholder UUID
    # in the canonical reasoning_details position (the empirical shape).
    bad = _synthetic_envelope_response("MALFORMED_FUNCTION_CALL", include_tool_call=True)
    bad.choices[0].message.provider_specific_fields = {
        "reasoning_details": [
            {"type": "reasoning.encrypted", "data": _OPENROUTER_PLACEHOLDER_UUID_B64}
        ]
    }
    with patch("tolokaforge.core.llm.client.completion", return_value=bad):
        with pytest.raises(RuntimeError):
            client.generate(
                system="s",
                messages=[Message(role=MessageRole.USER, content="hi")],
            )


# ---------------------------------------------------------------------------
# Defensive: the detection function must be importable from client.py
# ---------------------------------------------------------------------------


def test_detection_function_is_a_module_level_export() -> None:
    """Stable surface — the detection function is documented and callable."""
    assert callable(_detect_synthetic_envelope)
    # Doc-strings are part of the contract; this protects against silent
    # de-documentation when the function is refactored.
    doc: str | None = _detect_synthetic_envelope.__doc__
    assert doc is not None and "synthetic" in doc.lower()


# ---------------------------------------------------------------------------
# Defensive shape coverage — extras that would mask the marker
# ---------------------------------------------------------------------------


@pytest.fixture(name="placeholder_artifacts")
def _placeholder_artifacts() -> list[dict[str, Any]]:
    """Real placeholder reasoning_details captured during the OTS eval."""
    return [
        {
            "type": "reasoning.encrypted",
            "data": _OPENROUTER_PLACEHOLDER_UUID_B64,
            "format": "google-gemini-v1",
        }
    ]


def test_detection_short_circuits_before_reasoning_codec_runs(
    monkeypatch: pytest.MonkeyPatch,
    placeholder_artifacts: list[dict[str, Any]],
) -> None:
    """The synthetic envelope must be caught before reasoning extraction —
    otherwise the placeholder UUID may pollute a partial ``StructuredReasoning``."""
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)

    bad = _synthetic_envelope_response("MALFORMED_FUNCTION_CALL")
    bad.choices[0].message.provider_specific_fields = {"reasoning_details": placeholder_artifacts}

    with patch("tolokaforge.core.llm.client.completion", return_value=bad):
        with pytest.raises(RuntimeError):
            client.generate(
                system="s",
                messages=[Message(role=MessageRole.USER, content="hi")],
            )
