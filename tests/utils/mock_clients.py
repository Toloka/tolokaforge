"""
Shared mock HTTP clients for testing.

This module provides mock async HTTP clients that wrap FastAPI's TestClient
for sync-to-async bridging. This is the **canonical** source for MockAsyncClient.

Usage::

    from tests.utils.mock_clients import MockAsyncClient
"""

import httpx
from starlette.testclient import TestClient


class MockAsyncClient:
    """
    Mock async HTTP client that wraps FastAPI TestClient for sync-to-async bridging.

    This allows clients that use httpx.AsyncClient (like DBServiceClient) to work with
    FastAPI's TestClient (which is synchronous).

    Usage:
        from fastapi.testclient import TestClient
        from tests.utils.mock_clients import MockAsyncClient

        test_client = TestClient(app)
        mock_client = MockAsyncClient(test_client, "http://test")
        db_client = DBServiceClient(client=mock_client, base_url="http://test")
    """

    def __init__(self, test_client: TestClient, base_url: str):
        """
        Initialize the mock async client.

        Args:
            test_client: FastAPI TestClient instance
            base_url: Base URL for the client (used for compatibility)
        """
        self.test_client = test_client
        self.base_url = base_url
        self.is_closed = False

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """Perform GET request via TestClient."""
        response = self.test_client.get(url, **kwargs)
        return self._wrap_response(response)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """Perform POST request via TestClient."""
        response = self.test_client.post(url, **kwargs)
        return self._wrap_response(response)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        """Perform PUT request via TestClient."""
        response = self.test_client.put(url, **kwargs)
        return self._wrap_response(response)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        """Perform DELETE request via TestClient."""
        response = self.test_client.delete(url, **kwargs)
        return self._wrap_response(response)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        """Perform PATCH request via TestClient."""
        response = self.test_client.patch(url, **kwargs)
        return self._wrap_response(response)

    async def aclose(self) -> None:
        """Close the client."""
        self.is_closed = True

    def _wrap_response(self, response) -> httpx.Response:
        """Wrap TestClient response as httpx.Response."""
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
        )
