"""Stage 8 capability test — :attr:`Capability.USAGE_METRICS_POPULATED`.

Guards P7: every live call must populate the Stage 5
:class:`~tolokaforge.core.llm.usage.Usage` surface (``prompt_tokens``,
``completion_tokens``, ``provider_raw``). For Anthropic-family models
the cache-accounting fields (``cache_creation_input_tokens`` /
``cache_read_input_tokens``) must at minimum be present (``>= 0``) even
on the first call before Stage 6's ephemeral cache warms up.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_usage_metrics_populated(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    skip_unless_capability_declared(cert, Capability.USAGE_METRICS_POPULATED)
    client = live_client(cert)
    result = client.generate(
        system="You are a helpful assistant.",
        messages=[Message(role=MessageRole.USER, content="Say 'ok'.")],
    )

    usage = result.usage
    _msg = f"{cert.model_id}: prompt_tokens == 0 (provider_raw={usage.provider_raw!r})"
    assert usage.prompt_tokens > 0, _msg
    _msg = f"{cert.model_id}: completion_tokens == 0 (provider_raw={usage.provider_raw!r})"
    assert usage.completion_tokens > 0, _msg
    assert usage.provider_raw, (
        f"{cert.model_id}: provider_raw forensic block is empty — extractor "
        "silently dropped the response usage object"
    )

    # Anthropic-family models: cache-accounting fields must exist and be
    # non-negative. They're zero on the very first call (pre-warm); the
    # strict positive case is guarded by test_prompt_caching.
    if "claude" in cert.name.lower() or "anthropic" in cert.provider.lower():
        _msg = f"{cert.model_id}: cache_creation_input_tokens must be non-negative"
        assert usage.cache_creation_input_tokens >= 0, _msg
        _msg = f"{cert.model_id}: cache_read_input_tokens must be non-negative"
        assert usage.cache_read_input_tokens >= 0, _msg
