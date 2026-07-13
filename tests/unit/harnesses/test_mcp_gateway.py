"""Authenticated per-trial Runner MCP gateway behavior."""

from __future__ import annotations

import socket

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tolokaforge.runner.mcp_gateway import RunnerMCPGateway

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def gateway():
    instance = RunnerMCPGateway(host="127.0.0.1", port=_free_port())
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _local_url(gateway: RunnerMCPGateway, namespace: str) -> str:
    return f"http://127.0.0.1:{gateway.port}/mcp/{namespace}"


@pytest.mark.asyncio
async def test_gateway_lists_and_executes_tools(gateway: RunnerMCPGateway) -> None:
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return f"noted:{arguments['text']}", False

    registration = gateway.register(
        "notes:0",
        [
            {
                "name": "add_note",
                "description": "Add a note",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ],
        execute,
    )
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {registration.bearer_token}"}
    ) as client:
        async with streamable_http_client(
            _local_url(gateway, registration.namespace), http_client=client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("add_note", {"text": "first"})

    assert [tool.name for tool in tools.tools] == ["add_note"]
    assert result.isError is False
    assert result.content[0].text == "noted:first"
    assert calls == [("add_note", {"text": "first"})]


@pytest.mark.asyncio
async def test_gateway_rejects_missing_wrong_and_cross_trial_tokens(
    gateway: RunnerMCPGateway,
) -> None:
    async def execute(name, arguments):
        return "ok", False

    first = gateway.register("notes:0", [], execute)
    second = gateway.register("notes:1", [], execute)
    first_url = _local_url(gateway, first.namespace)

    async with httpx.AsyncClient() as client:
        missing = await client.post(first_url, json={})
        wrong = await client.post(first_url, headers={"Authorization": "Bearer wrong"}, json={})
        cross_trial = await client.post(
            first_url,
            headers={"Authorization": f"Bearer {second.bearer_token}"},
            json={},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert cross_trial.status_code == 401
    assert first.bearer_token != second.bearer_token


@pytest.mark.asyncio
async def test_unregister_removes_namespace(gateway: RunnerMCPGateway) -> None:
    async def execute(name, arguments):
        return "ok", False

    registration = gateway.register("notes:0", [], execute)
    gateway.unregister("notes:0")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            _local_url(gateway, registration.namespace),
            headers={"Authorization": f"Bearer {registration.bearer_token}"},
            json={},
        )

    assert response.status_code == 404
