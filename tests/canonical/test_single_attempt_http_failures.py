"""Single-attempt calls use one actual HTTP request, including timeout/refusal."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.utils.recording_gateway import (
    GatewayReply,
    server_error_reply,
    serving_recording_gateway,
    text_reply,
)
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.canonical


@pytest.mark.parametrize(
    "reply",
    [
        GatewayReply(
            400, {"error": {"message": "temperature refused", "type": "invalid_request_error"}}
        ),
        server_error_reply(),
        replace(text_reply("late"), delay_seconds=2),
    ],
    ids=["refusal", "transport", "timeout"],
)
def test_single_attempt_does_not_retry_a_real_http_failure(reply):
    with serving_recording_gateway() as gateway:
        gateway.catalog = ["openai/gpt-4.1"]
        gateway.scripts = {"gpt-4.1": [reply, text_reply("must not be reached")]}
        with secret_manager_installed(
            {
                "LLM_PROXY_BASE_URL": gateway.base_url,
                "LLM_PROXY_API_KEY": "loopback-only",
            }
        ):
            client = LLMClient(
                ModelConfig(
                    provider="openrouter",
                    name="openai/gpt-4.1",
                    temperature=0.0,
                    capabilities={"api_call_timeout_s": 1},
                )
            )
            with pytest.raises(RuntimeError):
                client.generate(
                    system="Synthetic judge",
                    messages=[
                        Message(role=MessageRole.USER, content="Evaluate this synthetic input")
                    ],
                    tools=[],
                    tool_choice="none",
                    response_format={"type": "json_object"},
                    retry_policy="single_attempt",
                )
        assert len(gateway.requests) == 1
        body = gateway.requests[0].body
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0.0
