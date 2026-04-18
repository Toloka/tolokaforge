"""Security validation tests for Docker container network isolation.

Tests container escape attempts and network isolation.
Tests use testcontainers for automatic lifecycle management, then configure
network isolation within the test to verify the runner cannot reach the
external internet.
"""

import pytest

import docker

pytestmark = pytest.mark.integration


@pytest.mark.requires_docker
@pytest.mark.docker
class TestNetworkIsolation:
    """Test network isolation between containers using testcontainers.

    Testcontainers start containers on a bridged network for health checks.
    This test creates an additional internal (isolated) network, moves the
    runner onto it, and verifies it cannot reach the external internet while
    still being able to communicate with the db-service.
    """

    def test_runner_cannot_access_external_network(self, runner_container, json_db_container):
        """Runner should only access internal services, not external internet."""
        client = docker.from_env()

        # Get low-level container objects
        runner_id = runner_container._container.id
        db_id = json_db_container._container.id

        # Create an internal network (blocks external internet access)
        internal_net = client.networks.create(
            "test-internal-net",
            driver="bridge",
            internal=True,
            labels={"managed_by": "testcontainers"},
        )
        try:
            # Connect both containers to the internal network with aliases
            internal_net.connect(db_id, aliases=["db-service", "json-db"])
            internal_net.connect(runner_id, aliases=["runner"])

            # Find and disconnect the runner from all NON-internal networks
            # (bridged networks that provide internet access)
            runner_info = client.containers.get(runner_id)
            for net_name, net_info in runner_info.attrs["NetworkSettings"]["Networks"].items():
                if net_name != "test-internal-net":
                    try:
                        net_obj = client.networks.get(net_info["NetworkID"])
                        net_obj.disconnect(runner_id)
                    except Exception:
                        pass

            # Now verify: runner can reach db-service on the internal network
            exit_code, output = runner_container.exec(
                ["curl", "-f", "--max-time", "5", "http://db-service:8000/health"]
            )
            assert exit_code == 0, f"Runner cannot reach db-service on internal network: {output}"

            # Verify: runner can NOT reach the external internet
            exit_code, output = runner_container.exec(
                ["curl", "-f", "--max-time", "5", "http://google.com"]
            )
            assert exit_code != 0, (
                "SECURITY VIOLATION: Runner container can access external internet. "
                "Network isolation is not properly configured."
            )
        finally:
            # Cleanup: remove internal network (disconnect containers first)
            try:
                internal_net.disconnect(runner_id, force=True)
            except Exception:
                pass
            try:
                internal_net.disconnect(db_id, force=True)
            except Exception:
                pass
            try:
                internal_net.remove()
            except Exception:
                pass
