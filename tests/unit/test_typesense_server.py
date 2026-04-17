"""Unit tests for TypeSense server management."""

import tempfile
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.models import TypeSenseConfig

pytestmark = pytest.mark.unit


class TestTypeSenseServerHelpers:
    """Tests for TypeSense server helper functions."""

    def test_find_free_port(self):
        """Test that find_free_port returns a valid port."""
        from tolokaforge.core.search.typesense_server import find_free_port

        port = find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_generate_api_key(self):
        """Test that generate_api_key produces valid keys."""
        from tolokaforge.core.search.typesense_server import generate_api_key

        key = generate_api_key()
        assert isinstance(key, str)
        assert len(key) > 20  # Should be reasonably long


class TestTypeSenseServerManagerDockerNotAvailable:
    """Tests for TypeSense server when Docker is not available."""

    def test_create_typesense_server_no_docker(self):
        """Test server creation fails gracefully without Docker."""
        from tolokaforge.core.search.typesense_server import (
            DOCKER_AVAILABLE,
            create_typesense_server,
        )

        if not DOCKER_AVAILABLE:
            config = TypeSenseConfig()
            server = create_typesense_server(config)
            assert server is None


@pytest.mark.skipif(
    not pytest.importorskip("docker", reason="Docker SDK not available"),
    reason="Docker SDK not available",
)
class TestTypeSenseServerManagerWithMockedDocker:
    """Tests for TypeSense server manager with mocked Docker client."""

    @pytest.fixture
    def mock_docker_client(self):
        """Create a mock Docker client."""
        client = MagicMock()

        # Mock container
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.attrs = {"State": {"Running": True}}
        mock_container.id = "test-container-123"

        # Mock containers.run
        client.containers.run.return_value = mock_container

        # Mock containers.list (no existing container)
        client.containers.list.return_value = []

        return client

    @pytest.fixture
    def mock_docker_from_env(self, mock_docker_client):
        """Mock docker.from_env()."""
        with patch("docker.from_env", return_value=mock_docker_client):
            yield mock_docker_client

    def test_server_manager_init(self, mock_docker_from_env):
        """Test TypeSenseServerManager initialization."""
        from tolokaforge.core.search.typesense_server import TypeSenseServerManager

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = TypeSenseServerManager(
                host="127.0.0.1",
                port=8108,
                api_key="test-key",
                data_dir=tmpdir,
            )
            assert manager.host == "127.0.0.1"
            assert manager.api_key == "test-key"
            # Port is not resolved until start() is called for "auto", but
            # for explicit port it's stored in _requested_port
            assert manager._requested_port == 8108

    def test_server_manager_auto_port(self, mock_docker_from_env):
        """Test that auto port is resolved when start() is called."""
        from tolokaforge.core.search.typesense_server import TypeSenseServerManager

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = TypeSenseServerManager(
                port="auto",
                api_key="test-key",
                data_dir=tmpdir,
            )
            # Before start, port is -1
            assert manager.port == -1
            assert manager._requested_port == "auto"

            # _resolve_port() should return a valid port
            resolved = manager._resolve_port()
            assert isinstance(resolved, int)
            assert 1024 <= resolved <= 65535

    def test_server_manager_auto_api_key(self, mock_docker_from_env):
        """Test that api_key is auto-generated when None."""
        from tolokaforge.core.search.typesense_server import TypeSenseServerManager

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = TypeSenseServerManager(
                port=8108,
                api_key=None,  # Should be auto-generated
                data_dir=tmpdir,
            )
            assert manager.api_key is not None
            assert len(manager.api_key) > 20
