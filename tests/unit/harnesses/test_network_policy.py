"""BYOH derived allowlists and proxy environment contracts."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.models import AgentHarnessConfig, AgentNetworkConfig
from tolokaforge.harnesses.forward_proxy import Allowlist, handle
from tolokaforge.harnesses.network import effective_network_policy
from tolokaforge.harnesses.registry import get_harness_spec
from tolokaforge.harnesses.trial_runner import HarnessTrialRunner

pytestmark = pytest.mark.unit


def test_default_policy_derives_provider_and_install_hosts() -> None:
    policy = effective_network_policy(AgentNetworkConfig(), get_harness_spec("claude-code"))

    assert policy.mode == "allowlist"
    assert policy.entries == ["api.anthropic.com", "registry.npmjs.org"]


def test_explicit_no_network_is_preserved() -> None:
    configured = AgentNetworkConfig(mode="no-network")

    assert effective_network_policy(configured, get_harness_spec("claude-code")) is configured


def test_proxy_variables_are_injected_without_overriding_no_proxy(tmp_path) -> None:
    runner = HarnessTrialRunner(
        AgentHarnessConfig(type="claude-code", version="2.1.203"),
        network=MagicMock(),
        workspace_root=tmp_path,
        episode_timeout_s=30,
        proxy_url="http://agent-proxy:8080",
    )
    image = MagicMock()
    image.name = "agent"
    image.full_tag = "agent:latest"

    with (
        patch("tolokaforge.harnesses.trial_runner.Image.build", return_value=image),
        patch("tolokaforge.harnesses.trial_runner.Container.create") as create,
    ):
        runner._create_container("task", 0, tmp_path)

    environment = create.call_args.kwargs["environment"]
    assert environment["HTTPS_PROXY"] == "http://agent-proxy:8080"
    assert environment["NO_PROXY"] == "runner,localhost,127.0.0.1"


@pytest.mark.asyncio
async def test_forward_proxy_allows_listed_destination_and_audits_denial(capsys) -> None:
    async def origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while await reader.readline() not in {b"\r\n", b"\n", b""}:
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = origin_server.sockets[0].getsockname()[1]
    allowlist = Allowlist(["127.0.0.1/32"])
    proxy_server = await asyncio.start_server(
        lambda reader, writer: handle(reader, writer, allowlist), "127.0.0.1", 0
    )
    proxy_port = proxy_server.sockets[0].getsockname()[1]

    async def request(url: str) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"GET {url} HTTP/1.1\r\nHost: ignored\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        return response

    try:
        allowed = await request(f"http://127.0.0.1:{origin_port}/health")
        denied = await request("http://example.invalid/private")
    finally:
        proxy_server.close()
        origin_server.close()
        await proxy_server.wait_closed()
        await origin_server.wait_closed()

    assert b"200 OK" in allowed and allowed.endswith(b"ok")
    assert b"403 Forbidden" in denied
    audit_output = capsys.readouterr().out
    assert '"status": "allowed"' in audit_output
    assert '"status": "denied"' in audit_output
