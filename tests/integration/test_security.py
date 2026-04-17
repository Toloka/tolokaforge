"""Security validation tests for Docker container network isolation.

Tests container escape attempts and network isolation.
Tests use testcontainers for automatic lifecycle management.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.requires_docker
@pytest.mark.docker
class TestNetworkIsolation:
    """Test network isolation between containers using testcontainers"""

    def test_runner_cannot_access_external_network(self, runner_container, json_db_container):
        """Runner should only access runner-net, not external internet"""
        # Containers are automatically started and healthy by fixtures

        # Runner should be able to reach db-service (on runner-net)
        exit_code, output = runner_container.exec(["curl", "-f", "http://db-service:8000/health"])
        assert exit_code == 0, f"Runner cannot reach runner-net services: {output}"

        # Runner should NOT be able to reach external internet
        exit_code, output = runner_container.exec(
            ["curl", "-f", "--max-time", "5", "http://google.com"]
        )
        assert exit_code != 0, (
            "SECURITY VIOLATION: Runner container can access external internet. "
            "Network isolation is not properly configured."
        )
