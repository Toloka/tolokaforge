"""Capability probe — :attr:`Capability.REASONING_EFFORT_HONOURED`.

Every effort level the engine can request (``low`` / ``medium`` / ``high``)
must still produce reasoning on the route UNDER A HEAVY AGENTIC CONTEXT:
the long policy system prompt, ~20 nested tool schemas and ten prior turns
of :mod:`._heavy_context`, ending in a user turn that needs a create call
carrying a mandated field. Reasoning is measured as ``usage.reasoning_tokens``
against a floor (or, for routes that report no token accounting, surfaced
reasoning text of comparable length).

Why heavy, and why a separate probe from :mod:`test_thinking_emits_blocks`:
``z-ai/glm-5.3`` via OpenRouter thinks normally at ``medium`` on a short
prompt (100–190 reasoning tokens) yet degrades to 0–54 tokens — and drops
the mandated field 15/15 — once the context looks like an evaluation turn
(2026-08-24/25; the Arena v1 eval: 0 reasoning tokens over 704 trials).
The short-prompt probe passes on that route; this one does not.

Per-model tuning through ``cert.probe_params[Capability.REASONING_EFFORT_HONOURED]``:
``efforts`` (levels to probe, default low/medium/high), ``attempts`` (calls
per level before it counts as degraded, default 2), ``min_reasoning_tokens``
(the floor, default :data:`MIN_REASONING_TOKENS`).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import ModelConfig
from tolokaforge.secrets import get_default
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

from ._heavy_context import MESSAGES, SYSTEM_PROMPT, TOOLS, mandated_field_present

_DEFAULT_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
_DEFAULT_ATTEMPTS = 2

#: Reasoning tokens below which a requested effort counts as not honoured.
#: Calibrated 2026-08-25 on this context: a degraded z-ai/glm-5.3 at
#: ``medium`` reported 0–54; the same route at its default 200–420; z-ai/glm-5.2
#: at ``medium`` well above. Chosen with margin on both sides.
MIN_REASONING_TOKENS = 100

#: When the provider reports no reasoning-token accounting at all, surfaced
#: reasoning text of at least this many characters stands in for the floor.
MIN_SURFACED_CHARS = 400


def _probe_settings(cert: ModelCertificate) -> tuple[tuple[str, ...], int, int]:
    params = cert.probe_params.get(Capability.REASONING_EFFORT_HONOURED, {})
    efforts = tuple(params.get("efforts", _DEFAULT_EFFORTS))
    attempts = int(params.get("attempts", _DEFAULT_ATTEMPTS))
    floor = int(params.get("min_reasoning_tokens", MIN_REASONING_TOKENS))
    return efforts, attempts, floor


def _reasoning_evidence(result) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """``(reasoning_tokens, surfaced_chars)`` — the two signals that thinking
    happened: the provider's usage accounting and the codec's surfaced text."""
    tokens = int(getattr(result.usage, "reasoning_tokens", 0) or 0)
    reasoning = result.reasoning
    chars = 0
    if reasoning is not None:
        chars = len(reasoning.summary or "") + sum(len(b.text or "") for b in reasoning.blocks)
    return tokens, chars


def _honoured(tokens: int, chars: int, floor: int) -> bool:
    return tokens >= floor or (tokens == 0 and chars >= MIN_SURFACED_CHARS)


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_reasoning_effort_honoured(
    cert: ModelCertificate,
    skip_unless_capability_declared,
) -> None:
    """Every probed effort level clears the reasoning floor on at least one attempt."""
    skip_unless_capability_declared(cert, Capability.REASONING_EFFORT_HONOURED)

    if not get_default().get_secret(cert.env_key):
        pytest.skip(f"{cert.env_key} not set — skipping live test for {cert.model_id}.")

    efforts, attempts, floor = _probe_settings(cert)
    client = LLMClient(ModelConfig(provider=cert.provider, name=cert.name))

    degraded: dict[str, list[dict[str, object]]] = {}
    for effort in efforts:
        observed: list[dict[str, object]] = []
        for _attempt in range(attempts):
            result = client.generate(
                system=SYSTEM_PROMPT,
                messages=MESSAGES,
                tools=TOOLS,
                tool_choice="auto",
                reasoning=ReasoningConfig(mode="adaptive", effort_hint=effort),  # type: ignore[arg-type]
                max_tokens=4000,
            )
            tokens, chars = _reasoning_evidence(result)
            observed.append(
                {
                    "reasoning_tokens": tokens,
                    "surfaced_chars": chars,
                    "mandated_field": mandated_field_present(result.tool_calls),
                }
            )
            if _honoured(tokens, chars, floor):
                break
        else:
            degraded[effort] = observed

    assert not degraded, (
        f"{cert.model_id}: reasoning degraded under a heavy context at effort level(s) "
        f"{sorted(degraded)} (floor {floor} reasoning tokens) — per attempt: {degraded}. "
        "The route did not honour the requested effort: a provider that redefines its "
        "effort vocabulary degrades an unknown level to no thinking instead of rejecting "
        "it, and the model then drops mandated tool fields. Declare a param_value_rules "
        "drop/override/reject for reasoning_effort on the model's preset "
        "(tolokaforge_models/data/model_presets.yaml) and re-probe."
    )
