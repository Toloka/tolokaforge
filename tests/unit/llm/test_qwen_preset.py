"""Qwen preset routing — passthrough schema + dict-map hints + JSON coercion.

Pre-Stage-2: Qwen fell through to ``default`` (no schema sanitisation, no
hints) and stringified every dict-map argument.

Post-Stage-2 (commit 8a591d5c3): Qwen used ``StrictSchema`` + ``DictMapHints``
+ ``ArrayDictMapResponse``. **This was wrong** — the schema rewrote
``Dict[str, T]`` to an array shape that contradicted the dict-format hint
**and** the task author's own ``system_prompt.md`` examples. The
``tau_manufacturing_v2`` post-fix diagnosis showed Qwen *never* picked the
array shape (0 of 1058 calls), instead emitting native dict (252) or
stringified JSON (806).

Post-bugfix (this commit): Qwen uses **passthrough** schema (so the model
sees the native ``additionalProperties`` shape it understands) + the
existing ``DictMapHints`` (which still surface alongside) + the new
**``json_coerce``** response policy (which recovers stringified JSON
arguments). Hints, schema, and task docs now all agree on dict shape.

This test pins the corrected declarative routing. It does NOT introduce a
Python-level ``qwen`` conditional — it asserts that
[`tolokaforge/core/data/model_presets.yaml`](../../../tolokaforge/core/data/model_presets.yaml)
contains a ``qwen`` preset that picks up every Qwen alias we expect.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import (
    AnthropicContent,
    DictMapHints,
    JsonCoerceResponse,
    OpenAIContent,
    PassthroughSchema,
    StandardResponse,
    StrictSchema,
    build_capabilities,
)

pytestmark = pytest.mark.unit


def test_qwen_openrouter_prefixed_hits_passthrough_trio() -> None:
    """``qwen/qwen3.6-plus`` via ``openrouter`` gets passthrough schema +
    dict-map hints + JSON coercion."""
    caps = build_capabilities("qwen/qwen3.6-plus", "openrouter")

    assert isinstance(caps.schema_sanitizer, PassthroughSchema)
    assert isinstance(caps.prompt_policy, DictMapHints)
    assert isinstance(caps.response_policy, JsonCoerceResponse)
    # OpenAI-shaped tool content — Anthropic preset must NOT have caught us.
    assert isinstance(caps.content_policy, OpenAIContent)


def test_qwen3_bare_name_matches_qwen3_glob() -> None:
    """``qwen3-max`` (no ``qwen/`` prefix) still lands on the qwen preset."""
    caps = build_capabilities("qwen3-max", "openrouter")

    assert isinstance(caps.schema_sanitizer, PassthroughSchema)
    assert isinstance(caps.prompt_policy, DictMapHints)
    assert isinstance(caps.response_policy, JsonCoerceResponse)


def test_qwen_schema_sanitizer_is_passthrough_not_strict() -> None:
    """Strict-schema's array conversion contradicted the dict-map hint
    and the task author's docs. The corrected preset routes through
    PassthroughSchema so model + hint + tool implementation all see
    ``additionalProperties`` (native dict-map)."""
    caps = build_capabilities("qwen/qwen3.6-plus", "openrouter")
    assert not isinstance(caps.schema_sanitizer, StrictSchema)
    assert isinstance(caps.schema_sanitizer, PassthroughSchema)


def test_qwen_does_not_hit_anthropic_preset() -> None:
    """The ``anthropic/*`` / ``*claude*`` globs must not accidentally match qwen."""
    caps = build_capabilities("qwen/qwen3.6-plus", "openrouter")
    assert not isinstance(caps.content_policy, AnthropicContent)


def test_non_qwen_model_does_not_hit_qwen_preset() -> None:
    """A hypothetical ``openai/qwen-adapter`` must NOT hit the qwen preset."""
    caps = build_capabilities("openai/qwen-adapter", "openrouter")

    assert not isinstance(caps.prompt_policy, DictMapHints)
    assert not isinstance(caps.response_policy, JsonCoerceResponse)


def test_qwen_response_policy_is_json_coerce_not_standard() -> None:
    """``JsonCoerceResponse`` must override the default ``StandardResponse``."""
    caps = build_capabilities("qwen/qwen3.6-plus", "openrouter")

    assert isinstance(caps.response_policy, JsonCoerceResponse)
    assert not isinstance(caps.response_policy, StandardResponse)
