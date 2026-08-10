"""Assistant-text slot — seam-proof + default passthrough.

Locks two contracts:

1. :class:`PassthroughAssistantText` (the shipped default) preserves
   assistant text byte-for-byte on every completion. Every existing preset
   yields the same ``GenerationResult.text`` after Stage 4 as before.
2. A subclass — defined here fixture-scope, NOT shipped in
   ``tolokaforge/core/llm/assistant_text_policy.py`` — can strip Cohere's
   ``<|START_TEXT|>…<|END_TEXT|>`` markers. Proves #929's Cohere unblock is
   expressible via the ``AssistantTextPolicy`` seam alone: a new policy
   subclass + a preset entry, no engine edits. The shipped Cohere subclass
   lands with the follow-up preset PR.

The seam threads all the way from YAML → :func:`build_capabilities` →
:meth:`LLMClient._assemble_result`, exercised in
:class:`TestSeamThreadsFromYAMLToAssembleResult`.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tolokaforge.core.llm import LLMClient
from tolokaforge.core.llm.assistant_text_policy import (
    AssistantTextPolicy,
    PassthroughAssistantText,
)
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.presets import (
    _ASSISTANT_TEXT_POLICIES,
    build_capabilities,
    resolve_policy_names,
    set_overlay_path,
)
from tolokaforge.core.models.model_config import ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture-scope Cohere-shape subclass — proves the seam covers #929
# ---------------------------------------------------------------------------
#
# NOT shipped in tolokaforge/core/llm/assistant_text_policy.py. Lives here so
# the seam-proof test can exercise the exact strip logic Cohere Command-A+
# needs (``<|START_TEXT|>…<|END_TEXT|>`` markers around the reply). The
# shipped subclass lands with the Cohere preset in #929.


_COHERE_MARKER_RE = re.compile(
    r"<\|START_TEXT\|>(?P<body>.*?)<\|END_TEXT\|>",
    re.DOTALL,
)


class CohereMarkerAssistantText:
    """Strips Cohere's ``<|START_TEXT|>…<|END_TEXT|>`` delimiters.

    Fixture-scope subclass — proves the seam is expressive enough to cover
    the Cohere Command-A+ shape. When multiple marker pairs are present the
    text between them is joined with a single space (mirrors the layout the
    Cohere provider emits: one text region per response block).
    """

    def parse_assistant_text(
        self,
        text: str,
        *,
        model_config: ModelConfig,  # noqa: ARG002
    ) -> str:
        bodies = _COHERE_MARKER_RE.findall(text)
        if not bodies:
            return text
        return " ".join(body.strip() for body in bodies)


def _response_with_text(text: str) -> SimpleNamespace:
    """Wrap ``text`` in the minimum litellm-response shape ``_assemble_result``
    reads: ``response.choices[0].message.content`` (no tool calls, no
    usage — the assistant-text hook only touches ``message.content``).
    """
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=text,
                    tool_calls=None,
                    reasoning_content=None,
                    thinking_blocks=None,
                    provider_specific_fields=None,
                )
            )
        ],
        usage=None,
        model="mock",
        _hidden_params={},
    )


# ---------------------------------------------------------------------------
# 1. Passthrough default — byte-identical for every existing preset
# ---------------------------------------------------------------------------


class TestPassthroughDefault:
    """The default :class:`PassthroughAssistantText` returns text unchanged."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Acknowledged. Task complete.",
            "Response with <|not_a_marker|> tokens in the middle.",
            "Multi-line\nresponse\nwith mixed content.",
            "​Zero-width unicode passes through verbatim.",
        ],
    )
    def test_passthrough_returns_text_unchanged(self, text: str) -> None:
        policy = PassthroughAssistantText()
        result = policy.parse_assistant_text(
            text, model_config=ModelConfig(provider="openrouter", name="openai/gpt-4o")
        )
        assert result == text

    @pytest.mark.parametrize(
        ("provider", "name"),
        [
            ("openrouter", "openai/gpt-4o"),
            ("openrouter", "anthropic/claude-opus-4.7"),
            ("openrouter", "google/gemini-3.1-pro-preview"),
            ("openrouter", "qwen/qwen3-next"),
            ("nova", "nova-pro"),
            ("xai", "x-ai/grok-4"),
        ],
    )
    def test_every_shipped_preset_resolves_to_passthrough(self, provider: str, name: str) -> None:
        caps = build_capabilities(name, provider)
        assert isinstance(caps.assistant_text_policy, PassthroughAssistantText)
        assert resolve_policy_names(caps)["assistant_text_policy"] == "passthrough"


# ---------------------------------------------------------------------------
# 2. Seam-proof — Cohere-shape subclass unblocks #929 without engine code
# ---------------------------------------------------------------------------


class TestCohereShapeAtAssembleResult:
    """The subclass strips markers when installed via
    :attr:`ModelCapabilities.assistant_text_policy`; the wire text landing on
    :class:`GenerationResult.text` is marker-free.
    """

    def _client_with_policy(self, policy: AssistantTextPolicy) -> LLMClient:
        config = ModelConfig(provider="mock", name="cohere/command-a-plus")
        client = LLMClient(config)
        object.__setattr__(
            client.capabilities,
            "assistant_text_policy",
            policy,
        )
        return client

    def test_markers_stripped_from_generation_result_text(self) -> None:
        client = self._client_with_policy(CohereMarkerAssistantText())
        wire = "prefix <|START_TEXT|>hello<|END_TEXT|> suffix"

        result = client._assemble_result(
            response=_response_with_text(wire),
            effective_system_prompt=None,
            latency_s=0.0,
        )

        assert result.text == "hello"

    def test_multi_marker_body_joined(self) -> None:
        client = self._client_with_policy(CohereMarkerAssistantText())
        wire = "<|START_TEXT|>first<|END_TEXT|> ignored <|START_TEXT|>second<|END_TEXT|>"

        result = client._assemble_result(
            response=_response_with_text(wire),
            effective_system_prompt=None,
            latency_s=0.0,
        )

        assert result.text == "first second"

    def test_text_without_markers_survives_verbatim(self) -> None:
        # A marker-free response must not be corrupted by the strip logic.
        client = self._client_with_policy(CohereMarkerAssistantText())
        wire = "A plain reply with no markers."

        result = client._assemble_result(
            response=_response_with_text(wire),
            effective_system_prompt=None,
            latency_s=0.0,
        )

        assert result.text == wire

    def test_default_passthrough_preserves_markers_when_no_policy_installed(
        self,
    ) -> None:
        # Every shipped preset resolves to PassthroughAssistantText — a
        # request whose provider is NOT Cohere must not have Cohere's marker
        # payload silently stripped, even if the model happens to emit that
        # substring in its response.
        config = ModelConfig(provider="openrouter", name="openai/gpt-4o")
        client = LLMClient(config)
        wire = "The delimiter <|START_TEXT|>inside<|END_TEXT|> is quoted text."

        result = client._assemble_result(
            response=_response_with_text(wire),
            effective_system_prompt=None,
            latency_s=0.0,
        )

        assert result.text == wire


# ---------------------------------------------------------------------------
# 3. Seam threads all the way from YAML → build_capabilities → _assemble_result
# ---------------------------------------------------------------------------


class TestSeamThreadsFromYAMLToAssembleResult:
    """Register the fixture-scope subclass in ``_ASSISTANT_TEXT_POLICIES``,
    reference it from a preset overlay via the ``{name, params}`` shape, and
    confirm the resolved capabilities on an ``LLMClient`` strip the markers on
    ``GenerationResult.text``. Locks the whole flow end to end.
    """

    def test_yaml_preset_installs_fixture_subclass_and_strips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(_ASSISTANT_TEXT_POLICIES, "cohere_markers", CohereMarkerAssistantText)

        overlay = tmp_path / "overlay.yaml"
        overlay.write_text(
            "presets:\n"
            "  cohere_command_a_plus:\n"
            "    match: ['cohere/*']\n"
            "    assistant_text_policy:\n"
            "      name: cohere_markers\n"
            "      params: {}\n"
        )
        set_overlay_path(str(overlay))
        try:
            caps = build_capabilities("cohere/command-a-plus", "cohere")
            assert isinstance(caps.assistant_text_policy, CohereMarkerAssistantText)
            assert resolve_policy_names(caps)["assistant_text_policy"] == "cohere_markers"

            client = LLMClient(ModelConfig(provider="cohere", name="cohere/command-a-plus"))
            wire = "chatter <|START_TEXT|>real reply<|END_TEXT|> trailing"
            result = client._assemble_result(
                response=_response_with_text(wire),
                effective_system_prompt=None,
                latency_s=0.0,
            )
            assert result.text == "real reply"
        finally:
            set_overlay_path(None)


# ---------------------------------------------------------------------------
# 4. ModelCapabilities carries the field with the correct default
# ---------------------------------------------------------------------------


def test_model_capabilities_default_is_passthrough() -> None:
    caps = ModelCapabilities()
    assert isinstance(caps.assistant_text_policy, PassthroughAssistantText)


def test_registry_ships_passthrough_as_only_entry() -> None:
    # #929's Cohere policy lands in a follow-up PR. This assertion is the
    # canary that flips when a Cohere-marker subclass ships in the engine.
    assert set(_ASSISTANT_TEXT_POLICIES) == {"passthrough"}
