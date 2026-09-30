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

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tolokaforge.core.llm import gateway_route
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.canonical

_EPHEMERAL = {"type": "ephemeral"}


class _RecordingGateway(ThreadingHTTPServer):
    """Serves ``GET /v1/models`` from :attr:`catalog` (500 when ``None``) and
    records every ``POST /v1/chat/completions`` body."""

    catalog: list[str] | None
    bodies: list[dict[str, Any]]


class _GatewayHandler(BaseHTTPRequestHandler):
    server: _RecordingGateway

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path != "/v1/models" or self.server.catalog is None:
            self._reply(500, {"error": "catalog unavailable"})
            return
        self._reply(200, {"data": [{"id": route} for route in self.server.catalog]})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._reply(404, {"error": f"unexpected path {self.path}"})
            return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.bodies.append(body)
        self._reply(
            200,
            {
                "id": "chatcmpl-loopback",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
            },
        )

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# Bound at import so the gateway URL is known when the secrets payload below is
# parametrised; the fixture serves and closes it.
_GATEWAY = _RecordingGateway(("127.0.0.1", 0), _GatewayHandler)
_GATEWAY_SECRETS = {
    "LLM_PROXY_BASE_URL": f"http://127.0.0.1:{_GATEWAY.server_port}/v1",
    "LLM_PROXY_API_KEY": "sk-loopback-gateway",
    "OPENROUTER_API_KEY": "sk-or-loopback",
}


@pytest.fixture(scope="module")
def _serving_gateway() -> Iterator[_RecordingGateway]:
    thread = threading.Thread(target=_GATEWAY.serve_forever, daemon=True)
    thread.start()
    yield _GATEWAY
    _GATEWAY.shutdown()
    _GATEWAY.server_close()
    thread.join()


@pytest.fixture
def gateway(_serving_gateway: _RecordingGateway) -> Iterator[_RecordingGateway]:
    _serving_gateway.catalog = None
    _serving_gateway.bodies = []
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


def _only_body(gateway: _RecordingGateway) -> dict[str, Any]:
    assert len(gateway.bodies) == 1, f"expected one chat completion, got {len(gateway.bodies)}"
    return gateway.bodies[0]


# The AnthropicEphemeralCache sites for the trajectory _generate sends: the last
# system block, the last tool, the first user message, and the tail tool
# message. messages[2] is the assistant turn and carries none.
_ANTHROPIC_MARKER_SITES = {
    ("messages", 0, "content", 0): _EPHEMERAL,
    ("messages", 1, "content", 0): _EPHEMERAL,
    ("messages", 3, "content", 0): _EPHEMERAL,
    ("tools", 1): _EPHEMERAL,
}


@pytest.mark.parametrize("installed_fake_secrets", [_GATEWAY_SECRETS], indirect=True)
def test_resolved_route_carries_the_anthropic_markers(gateway: _RecordingGateway) -> None:
    gateway.catalog = ["openrouter/anthropic/claude-sonnet-4.6", "openrouter/openai/gpt-5.2"]

    _generate(ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"))

    body = _only_body(gateway)
    assert body["model"] == "openrouter/anthropic/claude-sonnet-4.6"
    assert body["messages"][2]["role"] == "assistant"
    assert _marker_sites(body) == _ANTHROPIC_MARKER_SITES


@pytest.mark.parametrize("installed_fake_secrets", [_GATEWAY_SECRETS], indirect=True)
def test_unreadable_catalog_carries_the_anthropic_markers(gateway: _RecordingGateway) -> None:
    _generate(ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"))

    body = _only_body(gateway)
    assert body["model"] == "anthropic/claude-sonnet-4.6"
    assert body["messages"][2]["role"] == "assistant"
    assert _marker_sites(body) == _ANTHROPIC_MARKER_SITES


@pytest.mark.parametrize("installed_fake_secrets", [_GATEWAY_SECRETS], indirect=True)
def test_no_cache_preset_sends_no_markers(gateway: _RecordingGateway) -> None:
    gateway.catalog = ["openrouter/anthropic/claude-sonnet-4.6", "openrouter/openai/gpt-5.2"]

    # litellm's OpenAI transport refuses gpt-5 the config's default
    # ``temperature: 0.0``; ``None`` sends none.
    _generate(ModelConfig(provider="openrouter", name="openai/gpt-5.2", temperature=None))

    body = _only_body(gateway)
    assert body["model"] == "openrouter/openai/gpt-5.2"
    assert _marker_sites(body) == {}
