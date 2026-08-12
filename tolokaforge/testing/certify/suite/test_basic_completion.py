"""Stage 8 capability test — :attr:`Capability.BASIC_COMPLETION`.

Every registered model must return non-empty text for a trivial user
turn. Migrated from legacy ``tests/integration/test_llm_client_models.py::TestBasicCompletion``
and generalised over :data:`tolokaforge.testing.certify.ALL_MODELS`.

Assertions
----------
* ``result.text`` is non-empty (after strip).
* ``result.usage.prompt_tokens > 0`` — the provider returned a usage
  block (pairs with ``test_usage_metrics_populated`` but overlaps
  deliberately: ``BASIC_COMPLETION`` is the lowest-level signal, so
  losing observability here is also a regression).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_basic_completion(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    skip_unless_capability_declared(cert, Capability.BASIC_COMPLETION)
    client = live_client(cert)
    result = client.generate(
        system="You are a helpful assistant.",
        messages=[
            Message(role=MessageRole.USER, content="Say hello in one short sentence."),
        ],
    )
    assert result.text.strip(), f"{cert.model_id}: empty response"
    _msg = f"{cert.model_id}: missing prompt_tokens — provider_raw={result.usage.provider_raw!r}"
    assert result.usage.prompt_tokens > 0, _msg
