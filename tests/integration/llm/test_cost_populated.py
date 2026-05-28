"""Stage 8 capability test — :attr:`Capability.COST_USD_POPULATED`.

Asserts that every benchmarked call yields a positive ``result.cost_usd``.

The harness sources cost via the priority ladder in
:func:`tolokaforge.core.llm.client._litellm_response_cost`:

1. ``response._hidden_params["response_cost"]`` set by litellm.
2. :func:`litellm.completion_cost` re-derivation.
3. The bundled :data:`tolokaforge.core.pricing.MODEL_PRICING` table.

A live call coming back with ``cost_usd is None`` means none of those
three sources knew this model. The fix is a data-layer addition (update
``pricing.json`` or upgrade litellm), never an opt-out — so this is a
core capability and every certificate must list it as ``required``.

The bound (``< 1.0`` USD) is a sanity check: the canned single-turn
"Say 'ok'." prompt cannot legitimately exceed a fraction of a cent on
any model in the registry. Tripping it means we extracted a wildly
wrong number (e.g. multiplied by 1M twice somewhere along the line).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_cost_usd_populated(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    skip_unless_capability_declared(cert, Capability.COST_USD_POPULATED)
    client = live_client(cert)
    result = client.generate(
        system="You are a helpful assistant.",
        messages=[Message(role=MessageRole.USER, content="Say 'ok'.")],
    )

    cost = result.cost_usd
    _msg = (
        f"{cert.model_id}: cost_usd is None — neither litellm "
        f"(_hidden_params/completion_cost) nor pricing.json could price "
        f"this call. Fix by adding the model to pricing.json or by "
        f"upgrading litellm. Usage: prompt={result.usage.prompt_tokens}, "
        f"completion={result.usage.completion_tokens}, "
        f"provider_raw={result.usage.provider_raw!r}"
    )
    assert cost is not None, _msg

    _msg = (
        f"{cert.model_id}: cost_usd={cost} is non-positive — extractor "
        f"emitted zero/negative for a real call. Usage: "
        f"prompt={result.usage.prompt_tokens}, "
        f"completion={result.usage.completion_tokens}"
    )
    assert cost > 0.0, _msg

    # Sanity bound. A single-turn "Say 'ok'." should never approach a
    # full dollar on any model in the registry; values above this floor
    # almost always indicate a unit-conversion bug (per-token vs
    # per-1M-token mixed up). Update if a future model legitimately
    # exceeds it; "any positive number" alone is too weak a contract.
    assert cost < 1.0, (
        f"{cert.model_id}: cost_usd=${cost:.6f} for a one-shot 'Say ok' "
        "exceeds $1 — likely a unit-conversion bug in the cost engine."
    )
