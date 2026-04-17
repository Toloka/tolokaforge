"""Integration tests for TypeSense server lifecycle — requires Docker."""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
@pytest.mark.requires_docker
class TestTypeSenseServerIntegration:
    """Integration tests for TypeSense server — requires Docker daemon."""

    def test_full_server_lifecycle(self):
        """Test full server start/stop lifecycle."""
        from tolokaforge.core.search.typesense_server import (
            DOCKER_AVAILABLE,
            create_typesense_server,
        )

        if not DOCKER_AVAILABLE:
            pytest.skip("Docker SDK not installed")

        server = create_typesense_server(
            port="auto",
            data_dir=".cache/typesense-test",
            container_name="tolokaforge-typesense-test",
        )

        try:
            # Start server
            started = server.start()
            if not started:
                pytest.skip("Could not start TypeSense server")

            assert server.is_running()

            # Check we can connect
            import httpx

            response = httpx.get(
                f"http://{server.host}:{server.port}/health",
                headers={"X-TYPESENSE-API-KEY": server.api_key},
                timeout=5.0,
            )
            assert response.status_code == 200

        finally:
            # Stop server
            server.stop()
            assert not server.is_running()
