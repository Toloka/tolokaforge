"""
Docker Container Grading Verification Test.

Verifies Docker Runner containers are healthy and accessible.

The TlkMcpCore-specific grading comparison tests were removed in Stage H
because they depended on contrib/project-m-copilot-mock-tools data paths
(domains/external_retail_v3/testcases/) that do not exist — they would
always skip regardless of setup.

Requires Docker containers to be running:
    docker compose build db-service runner
    docker compose up -d db-service runner
"""

import pytest

from tests.utils.docker_helpers import DOCKER_RUNNER_ADDRESS

pytestmark = [pytest.mark.integration, pytest.mark.docker]

# =============================================================================
# Test Class
# =============================================================================


@pytest.mark.docker
class TestDockerGradingVerification:
    """
    Verify Docker Runner containers are healthy.

    These tests require Docker containers to be running:
        docker compose up -d db-service runner
    """

    def test_docker_containers_healthy(self, skip_if_no_docker_runner):
        """Verify Docker containers are running and healthy."""
        from tolokaforge.core.docker_runtime import RunnerClient

        client = RunnerClient(DOCKER_RUNNER_ADDRESS)
        client.connect(timeout=10)

        health = client.health_check_detailed()
        assert health["status"] == "healthy", f"Runner not healthy: {health}"
        assert health["db_service_connected"], "DB service not connected"

        client.close()


# =============================================================================
# Standalone Test Runner
# =============================================================================
