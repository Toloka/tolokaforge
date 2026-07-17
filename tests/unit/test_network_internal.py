"""`Network.internal` faithfully carries docker-py's `internal` flag, and the
built-in `EngineStack` always creates non-internal networks.

The second assertion is the executable form of "Case A (built-in stack) is
`full_internet` by construction" (ADR-0018): if a future change makes the
built-in network internal, the runner loses the LLM-provider egress it needs
for in-container LLM-as-judge grading, and this test fails.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _fake_docker_client() -> MagicMock:
    docker = pytest.importorskip("docker")
    client = MagicMock(spec=docker.DockerClient)
    client.networks.list.return_value = []
    created = MagicMock()
    created.id = "net123"
    client.networks.create.return_value = created
    return client


@pytest.mark.parametrize("internal", [True, False])
def test_network_create_round_trips_internal_flag(internal: bool) -> None:
    from tolokaforge.docker.network import Network

    client = _fake_docker_client()
    network = Network.create(name="edge-net", internal=internal, client=client)

    _, kwargs = client.networks.create.call_args
    assert kwargs["internal"] is internal
    assert network.internal is internal
    assert network.to_docker_network_config()["internal"] is internal


def test_engine_stack_creates_non_internal_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    import tolokaforge.docker.network as network_mod
    from tolokaforge.docker.stack import EngineStack, ServiceDefinition

    client = _fake_docker_client()
    monkeypatch.setattr(network_mod.docker, "from_env", lambda: client)

    stack = EngineStack()
    stack.add_service(
        ServiceDefinition(
            name="runner",
            image_name="tolokaforge-runner",
            networks=["runner-net"],
        )
    )

    networks = stack.create_networks()

    assert set(networks) == {"runner-net"}
    assert networks["runner-net"].internal is False
    _, kwargs = client.networks.create.call_args
    assert kwargs["name"] == "runner-net"
    assert kwargs["internal"] is False
