"""Verify Network.create() handles 409 Conflict race conditions.

When two processes call Network.create() concurrently, both can pass the
_find_existing_network() check before either calls create(). The second
create() then hits a 409 Conflict from the Docker daemon. The fix
catches the 409 and reuses the network the other process created.

Backports the contract from opensource commit d30a8d123.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def test_network_create_recovers_from_409_race() -> None:
    docker = pytest.importorskip("docker")
    from docker.errors import APIError

    from tolokaforge.docker.network import Network

    name = "env-net-race"
    raced_network = MagicMock(name="raced_network")
    raced_network.id = "race123"
    raced_network.name = name

    client = MagicMock(spec=docker.DockerClient)
    # Calls to _find_existing_network: first returns None (no network yet),
    # second (after 409) returns the network another process created.
    client.networks.list.side_effect = [
        [],  # initial _find_existing_network → None
        [raced_network],  # post-409 _find_existing_network → existing
    ]
    client.networks.create.side_effect = APIError("Conflict", response=MagicMock(status_code=409))

    network = Network.create(name=name, client=client)

    assert network.network_id == "race123"
    assert network.name == name
    assert client.networks.create.call_count == 1
    assert client.networks.list.call_count == 2


def test_network_create_propagates_non_409_api_errors() -> None:
    docker = pytest.importorskip("docker")
    from docker.errors import APIError

    from tolokaforge.docker.network import Network, NetworkError

    client = MagicMock(spec=docker.DockerClient)
    client.networks.list.return_value = []
    client.networks.create.side_effect = APIError(
        "Server error", response=MagicMock(status_code=500)
    )

    with pytest.raises(NetworkError):
        Network.create(name="env-net-broken", client=client)
