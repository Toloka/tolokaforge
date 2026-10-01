"""Contract: a preset's prompt-cache markers reach the LLM gateway on the wire.

An ``anthropic_ephemeral`` preset attaches ``cache_control: {type: ephemeral}``
markers before the request is handed to litellm, and litellm's transport then
decides what actually leaves the process. A gateway route resolved from the
catalog goes out through the OpenAI transport (``openrouter/<name>`` at the
gateway's ``api_base``); an unreadable catalog keeps the provider's own
transport. Either transport dropping the markers would leave every Claude call
through the gateway uncached — no error, just a prompt billed in full each turn.

Each case here drives the real :meth:`LLMClient.generate` against a loopback
server that plays the gateway, and inspects the body it received. A NoCache
preset through the same gateway is the control row: it proves the markers come
from the preset's policy, not from the transport.

If a case here fails after a litellm upgrade, the new litellm strips markers the
gateway needs. Do not edit the expected marker sites to match what litellm now
sends: raise the litellm floor past the regression, or pin below it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.utils.recording_gateway import RecordingGateway, serving_recording_gateway
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.core.llm import gateway_route
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.canonical

_EPHEMERAL = {"type": "ephemeral"}


@pytest.fixture(scope="module")
def _serving_gateway() -> Iterator[RecordingGateway]:
    with serving_recording_gateway() as server:
        yield server


@pytest.fixture
def installed_fake_secrets(_serving_gateway: RecordingGateway) -> Iterator[dict[str, str]]:
    """Point the process SecretManager at the loopback gateway."""
    payload = {
        "LLM_PROXY_BASE_URL": _serving_gateway.base_url,
        "LLM_PROXY_API_KEY": "sk-loopback-gateway",
        "OPENROUTER_API_KEY": "sk-or-loopback",
    }
    with secret_manager_installed(payload):
        yield payload


@pytest.fixture
def gateway(_serving_gateway: RecordingGateway) -> Iterator[RecordingGateway]:
    _serving_gateway.reset()
    gateway_route.clear_catalog_cache()
    yield _serving_gateway
    gateway_route.clear_catalog_cache()


def _generate(model: ModelConfig) -> None:
    """One agent turn with a string system prompt, two tools, and a
    user → assistant(tool_calls) → tool trajectory."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool,
                "description": f"{tool} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for tool in ("lookup_order", "refund_order")
    ]
    messages = [
        Message(role=MessageRole.USER, content="Refund order 7."),
        Message(
            role=MessageRole.ASSISTANT,
            content="Looking the order up.",
            tool_calls=[ToolCall(id="call_1", name="lookup_order", arguments={})],
        ),
        Message(role=MessageRole.TOOL, content='{"order": 7, "paid": true}', tool_call_id="call_1"),
    ]
    LLMClient(model).generate(system="You are a support agent.", messages=messages, tools=tools)


def _marker_sites(node: Any, path: tuple[str | int, ...] = ()) -> dict[tuple[str | int, ...], Any]:
    """Every ``cache_control`` value in the body, keyed by the path of the object carrying it."""
    sites: dict[tuple[str | int, ...], Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "cache_control":
                sites[path] = value
            else:
                sites.update(_marker_sites(value, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            sites.update(_marker_sites(value, (*path, index)))
    return sites


def _only_body(gateway: RecordingGateway) -> dict[str, Any]:
    assert len(gateway.requests) == 1, f"expected one chat completion, got {len(gateway.requests)}"
    return gateway.requests[0].body


# The AnthropicEphemeralCache sites for the trajectory _generate sends: the last
# system block, the last tool, the first user message, and the tail tool
# message. messages[2] is the assistant turn and carries none.
_ANTHROPIC_MARKER_SITES = {
    ("messages", 0, "content", 0): _EPHEMERAL,
    ("messages", 1, "content", 0): _EPHEMERAL,
    ("messages", 3, "content", 0): _EPHEMERAL,
    ("tools", 1): _EPHEMERAL,
}


def test_resolved_route_carries_the_anthropic_markers(gateway: RecordingGateway) -> None:
    gateway.catalog = ["openrouter/anthropic/claude-sonnet-4.6", "openrouter/openai/gpt-5.2"]

    _generate(ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"))

    body = _only_body(gateway)
    assert body["model"] == "openrouter/anthropic/claude-sonnet-4.6"
    assert body["messages"][2]["role"] == "assistant"
    assert _marker_sites(body) == _ANTHROPIC_MARKER_SITES


def test_unreadable_catalog_carries_the_anthropic_markers(gateway: RecordingGateway) -> None:
    _generate(ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"))

    body = _only_body(gateway)
    assert body["model"] == "anthropic/claude-sonnet-4.6"
    assert body["messages"][2]["role"] == "assistant"
    assert _marker_sites(body) == _ANTHROPIC_MARKER_SITES


def test_no_cache_preset_sends_no_markers(gateway: RecordingGateway) -> None:
    gateway.catalog = ["openrouter/anthropic/claude-sonnet-4.6", "openrouter/openai/gpt-5.2"]

    # litellm's OpenAI transport refuses gpt-5 the config's default
    # ``temperature: 0.0``; ``None`` sends none.
    _generate(ModelConfig(provider="openrouter", name="openai/gpt-5.2", temperature=None))

    body = _only_body(gateway)
    assert body["model"] == "openrouter/openai/gpt-5.2"
    assert _marker_sites(body) == {}
