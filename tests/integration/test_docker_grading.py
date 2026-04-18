"""
Docker Container Grading Verification Test.

Verifies Docker Runner containers are healthy and accessible.

Uses testcontainer fixtures for automatic container lifecycle management —
no manual ``docker compose up`` required.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.docker
class TestDockerGradingVerification:
    """
    Verify Docker Runner containers are healthy.

    Containers are auto-started via testcontainer fixtures
    (runner_container → json_db_container + rag_service_container).
    """

    def test_docker_containers_healthy(self, runner_container, json_db_container):
        """Verify Docker containers are running and healthy."""
        from tolokaforge.core.docker_runtime import RunnerClient

        host = runner_container.get_container_host_ip()
        port = runner_container.get_exposed_port(50051)
        runner_address = f"{host}:{port}"

        client = RunnerClient(runner_address)
        client.connect(timeout=10)

        health = client.health_check_detailed()
        assert health["status"] == "healthy", f"Runner not healthy: {health}"
        assert health["db_service_connected"], "DB service not connected"

        client.close()
