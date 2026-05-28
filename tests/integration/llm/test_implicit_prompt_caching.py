"""Capability test — :attr:`Capability.IMPLICIT_PROMPT_CACHING`.

Asserts the **provider-side auto-cache** surface: back-to-back calls with
an identical large system prompt + tools must hit an upstream cache that
fires WITHOUT us attaching explicit ``cache_control`` markers. Observable
via :attr:`~tolokaforge.core.llm.usage.Usage.cache_read_input_tokens` on
call 2 — which :class:`UsageExtractor` populates from the OpenAI-canonical
``prompt_tokens_details.cached_tokens`` path when the top-level Anthropic
counter is zero.

Distinct from :attr:`Capability.PROMPT_CACHING`: that contract is for
explicit Anthropic ephemeral caching and asserts BOTH
``cache_creation_input_tokens > 0`` (cold write) and
``cache_read_input_tokens > 0`` (warm read). OpenAI / DeepSeek routes on
OpenRouter auto-cache but do not surface a creation event on the cold
write — so this test omits that assertion and only checks call-2 reads.

Empirical motivation: OpenRouter routes to ``openai/*`` and
``deepseek/*`` reach ~80% cache hit (via ``cached_tokens`` in per-call
usage) while ``xiaomi/mimo*`` reports 0. The split makes the
route-specific contract testable in CI.

Parametrised over :data:`tests.integration.llm.registry.ALL_MODELS`.
Routes that genuinely don't auto-cache (mimo, kimi, anthropic — which
caches explicitly — qwen, grok, gemini, nova) declare the capability in
``known_unsupported``; the live test then skips.
"""

from __future__ import annotations

import uuid

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


def _large_system_prompt() -> str:
    """Build a ~8 k-token system prompt — comfortably above the upstream
    provider's auto-cache minimum (typically 1 k tokens for OpenAI,
    similar for DeepSeek). A per-call UUID prevents cross-test-run cache
    sharing so the contract is testable in isolation.
    """
    nonce = uuid.uuid4().hex
    preamble = (
        f"You are a meticulous on-call triage assistant. "
        f"Session nonce {nonce}: every prompt below is uniquely keyed to "
        f"this session and must not be confused with prior contexts. "
    )
    body = "Context: X. " * 2000  # ~4 tokens per rep → ~8 k tokens.
    return preamble + body


def _noop_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Return the current UTC timestamp.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_implicit_prompt_caching(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Two identical calls; the second must hit the upstream auto-cache.

    Assertions:

    1. Call 2 carries ``cache_read_input_tokens > 0``.
    2. The read counter is above 1 k tokens (defends against a stray
       provider-side reporting bug where a non-zero but tiny ``cached``
       value sneaks through despite no real cache hit).
    3. Total input-side accounting (raw + cached) stays in the same
       ballpark across the two calls — sanity check that the server
       actually credited the cached prefix rather than silently
       dropping it.
    4. Call 2 costs strictly less than call 1 — if the provider reports
       cache hits in the usage block but bills full input rate anyway,
       the cache is observable but worthless and the eval cost / latency
       numbers stay inflated. This is the assertion that catches a
       regression in :func:`tolokaforge.core.llm.client._litellm_response_cost`
       not honouring the cache-discounted rate.

    Note: we deliberately do NOT assert
    ``call1.cache_creation_input_tokens > 0`` — that's the explicit-cache
    contract covered by :attr:`Capability.PROMPT_CACHING`. OpenAI /
    DeepSeek routes auto-cache without surfacing a creation event.
    """
    skip_unless_capability_declared(cert, Capability.IMPLICIT_PROMPT_CACHING)
    client = live_client(cert)
    system = _large_system_prompt()
    tools = _noop_tools()

    result1 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content="Say 'one'.")],
        tools=tools,
        max_tokens=64,
    )
    result2 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content="Say 'two'.")],
        tools=tools,
        max_tokens=64,
    )

    assert result2.usage.cache_read_input_tokens > 0, (
        f"{cert.model_id}: second identical call must hit the upstream "
        f"auto-cache; got 0 cache_read_input_tokens "
        f"(provider_raw={result2.usage.provider_raw!r})"
    )
    _msg = (
        f"{cert.model_id}: cache_read_input_tokens "
        f"({result2.usage.cache_read_input_tokens}) must exceed the 1 k "
        f"threshold — anything smaller is provider-side reporting noise, "
        f"not a real cache hit on the ~8 k-token system prompt."
    )
    assert result2.usage.cache_read_input_tokens > 1000, _msg

    # Total input-side tokens (raw + cached) stay roughly aligned.
    # ``prompt_tokens`` on OpenRouter-routed OpenAI / DeepSeek includes
    # the cached portion (it's the wire-level input count, not the
    # billable input count), so cached + uncached doesn't always sum
    # cleanly. We accept a 30 % drift as a sanity floor — the goal is
    # to catch "server dropped the prefix" regressions, not lock in an
    # exact accounting shape that varies per provider.
    raw1 = result1.usage.prompt_tokens
    raw2 = result2.usage.prompt_tokens
    if raw1 > 0:
        drift = abs(raw1 - raw2) / raw1
        assert drift < 0.3, (
            f"{cert.model_id}: raw prompt_tokens drifted by {drift:.0%} "
            f"between identical calls (call1={raw1}, call2={raw2}); "
            f"cache accounting may be off."
        )

    assert result1.cost_usd is not None, (
        f"{cert.model_id}: cost_usd is None on call 1 — neither litellm "
        f"nor pricing.json could price the call "
        f"(provider_raw={result1.usage.provider_raw!r})"
    )
    assert (
        result2.cost_usd is not None
    ), f"{cert.model_id}: cost_usd is None on call 2 (provider_raw={result2.usage.provider_raw!r})"
    assert result2.cost_usd < result1.cost_usd, (
        f"{cert.model_id}: cache-read call did NOT cost less than the "
        f"initial call — auto-cache is reporting hits but the discounted "
        f"rate is not being applied. "
        f"call1: ${result1.cost_usd:.6f} "
        f"(prompt={result1.usage.prompt_tokens}, "
        f"cached={result1.usage.cached_tokens}, "
        f"cache_read={result1.usage.cache_read_input_tokens}); "
        f"call2: ${result2.cost_usd:.6f} "
        f"(prompt={result2.usage.prompt_tokens}, "
        f"cached={result2.usage.cached_tokens}, "
        f"cache_read={result2.usage.cache_read_input_tokens})"
    )
