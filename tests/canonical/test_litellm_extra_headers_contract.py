"""Contract: litellm puts ``extra_headers`` on the wire for both routed transports.

A model's session header reaches the backend only as an entry of the
``extra_headers`` kwarg the engine hands litellm. The gateway tests cover the
``openai`` transport a resolved gateway route uses; gateway-off OpenRouter goes
out through litellm's ``openrouter`` transport, which no gateway test reaches.
Each case calls :func:`litellm.completion` on one transport against a loopback
``api_base`` and reads the headers that arrived.

If a case here fails after a litellm upgrade, that transport no longer forwards
caller headers, and every session-affine backend behind it loses its affinity
silently. Raise the litellm floor past the regression or pin below it.
"""

from __future__ import annotations

import litellm
import pytest

from tests.utils.recording_gateway import RecordingGateway

pytestmark = pytest.mark.canonical


@pytest.mark.parametrize(
    "provider, model",
    [("openai", "self-hosted/canary"), ("openrouter", "anthropic/claude-sonnet-4.6")],
)
def test_extra_headers_reach_the_wire(gateway: RecordingGateway, provider: str, model: str) -> None:
    litellm.completion(
        model=model,
        custom_llm_provider=provider,
        api_base=gateway.base_url,
        api_key="sk-loopback",
        messages=[{"role": "user", "content": "ping"}],
        extra_headers={"x-session-id": "conversation-1"},
    )

    assert [(r.model, r.headers.get("x-session-id")) for r in gateway.requests] == [
        (model, "conversation-1")
    ]
