"""Capability test — :attr:`Capability.PROMPT_CACHING`.

Back-to-back generations with an identical large system prompt + tool
schemas must hit the provider-side ephemeral cache. Observable via
:attr:`tolokaforge.core.llm.usage.Usage.cache_creation_input_tokens`
(writes) and :attr:`.cache_read_input_tokens` (reads).

Without the
:class:`~tolokaforge.core.llm.cache_policy.AnthropicEphemeralCache`
wiring these counters stay at zero and Claude re-bills 18 k of system
prompt + 8 k of tool schemas per turn.

A unique-per-run nonce in the system prompt forces a cold cache write
on the first call so we can observe both the write *and* the read —
otherwise OpenRouter's automatic caching across test runs would have
served the second run from a warm cache and zeroed the cache_creation
counter.

Parametrised over :data:`tests.integration.llm.registry.ALL_MODELS`;
non-Anthropic certificates declare the capability in
``known_unsupported`` (OpenAI / Qwen / Grok presets carry
:class:`NoCache`). A canonical test pins that scoping.
"""

from __future__ import annotations

import uuid

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


def _large_system_prompt() -> str:
    """Build a ~8 k-token system prompt — comfortably above Anthropic's
    1 k-token minimum cache threshold so caching is actually invoked.

    A per-call UUID ensures every test run starts with a cold cache —
    otherwise OpenRouter's automatic caching across sessions would zero
    out ``cache_creation_input_tokens`` on the first call and the test
    would see only a read on call 2.
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
def test_prompt_caching(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """First call creates the cache; second identical call reads it.

    The two calls share the same ``system`` (built once outside the loop)
    so the cacheable prefix matches; the per-test nonce inside the prompt
    keeps the cache cold across test sessions.
    """
    skip_unless_capability_declared(cert, Capability.PROMPT_CACHING)
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

    assert result1.usage.cache_creation_input_tokens > 0, (
        f"{cert.model_id}: first call must create a cache entry; got 0 "
        f"cache_creation_input_tokens (provider_raw={result1.usage.provider_raw!r})"
    )
    assert result2.usage.cache_read_input_tokens > 0, (
        f"{cert.model_id}: second call must hit the cache; got 0 "
        f"cache_read_input_tokens (provider_raw={result2.usage.provider_raw!r})"
    )
    # Meaningful cache hit (above the 1 k-token threshold Anthropic enforces).
    _msg = f"{cert.model_id}: cache_read_input_tokens must exceed the 1 k threshold"
    assert result2.usage.cache_read_input_tokens > 1000, _msg

    # Total input-side tokens (cached + uncached) stay in the same ballpark
    # across the two calls — sanity check that the server actually counted
    # the cached prefix, not just silently dropped it.
    total1 = result1.usage.prompt_tokens + result1.usage.cache_creation_input_tokens
    total2 = result2.usage.prompt_tokens + result2.usage.cache_read_input_tokens
    assert abs(total1 - total2) < 0.2 * total1, (
        f"{cert.model_id}: total input tokens drifted by >20% between calls "
        f"(call1={total1}, call2={total2}); cache accounting may be off."
    )

    # End-to-end cost contract: a cache HIT must cost meaningfully LESS than
    # a cache MISS at the same prompt size. This is the specific behaviour
    # the cache-rate work in PR #104 was meant to deliver — without this
    # assertion, the cache path could regress to charging full input rate
    # on cached reads and we'd never notice from the token counters alone.
    #
    # The two calls share the same effective input prefix (the large system
    # prompt + tools), so cost1 (cache write — billed at full input rate
    # plus the cache_write surcharge) must exceed cost2 (cache read —
    # billed at the discounted cache_read rate, currently 10% of input on
    # Anthropic). The exact ratio depends on the priority tier that
    # produced each cost (litellm vs local pricing.json), so we use a
    # conservative floor: cost2 must be strictly less than cost1.
    assert result1.cost_usd is not None, (
        f"{cert.model_id}: cost_usd is None on cache-write call — neither "
        f"litellm nor pricing.json could price the call (provider_raw="
        f"{result1.usage.provider_raw!r})"
    )
    assert result2.cost_usd is not None, (
        f"{cert.model_id}: cost_usd is None on cache-read call — neither "
        f"litellm nor pricing.json could price the call (provider_raw="
        f"{result2.usage.provider_raw!r})"
    )
    assert result2.cost_usd < result1.cost_usd, (
        f"{cert.model_id}: cache-read call did NOT cost less than cache-write "
        f"call — cache discount is not being applied. "
        f"call1 (write): ${result1.cost_usd:.6f} "
        f"(prompt={result1.usage.prompt_tokens}, "
        f"cache_creation={result1.usage.cache_creation_input_tokens}, "
        f"cache_read={result1.usage.cache_read_input_tokens}); "
        f"call2 (read): ${result2.cost_usd:.6f} "
        f"(prompt={result2.usage.prompt_tokens}, "
        f"cache_creation={result2.usage.cache_creation_input_tokens}, "
        f"cache_read={result2.usage.cache_read_input_tokens})"
    )
