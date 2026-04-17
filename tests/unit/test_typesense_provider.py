"""Tests for TypeSense provider."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tolokaforge.core.search.typesense_provider import (
    TypeSenseProvider,
    TypeSenseProviderConfig,
)

pytestmark = pytest.mark.unit


class TestTypeSenseProvider:
    """Test TypeSenseProvider class."""

    def test_disabled_provider(self):
        """Test provider when disabled."""
        config = TypeSenseProviderConfig(enabled=False)
        provider = TypeSenseProvider(config)

        assert not provider.is_available()
        assert not provider.initialize_for_domain("test", ["doc1"])
        assert provider.get_client_for_domain("test") is None

        response = provider.search("test", "query")
        assert response.total_hits == 0
        assert len(response.hits) == 0

    def test_stub_provider(self):
        """Test provider with stub implementation."""
        config = TypeSenseProviderConfig(use_stub=True)
        provider = TypeSenseProvider(config)

        assert not provider.is_available()
        assert provider.initialize_for_domain("test", ["doc1"]) is True

        client = provider.get_client_for_domain("test")
        assert client is not None

    @patch("tolokaforge.core.search.typesense_provider.MCP_CORE_AVAILABLE", False)
    def test_no_mcp_core(self):
        """Test provider when mcp_core is not available."""
        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        assert not provider.is_available()
        assert provider.initialize_for_domain("test", ["doc1"]) is True  # Uses stub

        response = provider.search("test", "query")
        assert response.total_hits == 0

    def test_load_documents_from_directory(self):
        """Test loading documents from directory."""
        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        # Create temporary directory with test documents
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test markdown files
            (temp_path / "doc1.md").write_text("# Document 1\nContent of doc 1")
            (temp_path / "doc2.md").write_text("# Document 2\nContent of doc 2")
            (temp_path / "empty.md").write_text("")  # Empty file should be ignored
            (temp_path / "not_md.txt").write_text("Not markdown")  # Non-md file ignored

            documents = provider.load_documents_from_directory(temp_path)

            assert len(documents) == 2
            assert "# Document 1\nContent of doc 1" in documents
            assert "# Document 2\nContent of doc 2" in documents

    @patch("tolokaforge.core.search.typesense_provider.MCP_CORE_AVAILABLE", True)
    @patch("tolokaforge.core.search.typesense_provider.initialize_typesense_for_domain")
    def test_real_typesense_initialization_success(self, mock_init):
        """Test successful TypeSense initialization with real client."""
        # Setup mock
        mock_client = Mock()
        mock_client.is_available = True
        mock_init.return_value = mock_client

        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        assert provider.is_available()

        success = provider.initialize_for_domain("test", ["doc1", "doc2"])
        assert success is True

        mock_init.assert_called_once_with(
            domain="test",
            snippets=["doc1", "doc2"],
            host="127.0.0.1",
            port=8108,
            timeout=30.0,
            api_key=None,
        )

    @patch("tolokaforge.core.search.typesense_provider.MCP_CORE_AVAILABLE", True)
    @patch("tolokaforge.core.search.typesense_provider.initialize_typesense_for_domain")
    def test_real_typesense_initialization_failure(self, mock_init):
        """Test failed TypeSense initialization with real client."""
        # Setup mock to return unavailable client
        mock_client = Mock()
        mock_client.is_available = False
        mock_init.return_value = mock_client

        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        success = provider.initialize_for_domain("test", ["doc1"])
        assert success is False

    @patch("tolokaforge.core.search.typesense_provider.MCP_CORE_AVAILABLE", True)
    @patch("tolokaforge.core.search.typesense_provider.get_typesense_for_domain")
    def test_search_with_real_client(self, mock_get_client):
        """Test search with real TypeSense client."""
        # Setup mock client with search results
        mock_client = Mock()
        mock_client.universal_search_with_full_text.return_value = [
            {
                "source": "doc1",
                "score": 0.8,
                "text": "Document content",
                "vector_distance": 0.2,
            }
        ]
        mock_get_client.return_value = mock_client

        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        response = provider.search("test", "query", max_results=5)

        assert response.total_hits == 1
        assert len(response.hits) == 1
        assert response.hits[0].document_id == "doc1"
        assert response.hits[0].score == 0.8
        assert response.hits[0].content["text"] == "Document content"

        mock_client.universal_search_with_full_text.assert_called_once_with("query", keywords=[])

    @patch("tolokaforge.core.search.typesense_provider.MCP_CORE_AVAILABLE", True)
    @patch("tolokaforge.core.search.typesense_provider.get_typesense_for_domain")
    def test_search_with_no_client(self, mock_get_client):
        """Test search when no client is available."""
        mock_get_client.return_value = None

        config = TypeSenseProviderConfig()
        provider = TypeSenseProvider(config)

        response = provider.search("test", "query")

        assert response.total_hits == 0
        assert len(response.hits) == 0
