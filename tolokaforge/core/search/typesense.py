"""
TypeSense search backend interface.

This module provides an abstract interface for TypeSense full-text search,
used primarily by tool-use domains for knowledge base search functionality.

TODO: Implement actual TypeSense integration when needed.
Currently provides a stub implementation that returns empty results.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A single search result from TypeSense."""

    document_id: str
    """Unique identifier of the document."""

    score: float
    """Relevance score (higher is better)."""

    content: dict[str, Any]
    """The full document content."""

    highlights: dict[str, list[str]] = field(default_factory=dict)
    """Highlighted snippets matching the query, keyed by field name."""


@dataclass
class SearchResponse:
    """Response from a TypeSense search query."""

    hits: list[SearchResult]
    """List of matching documents."""

    total_hits: int
    """Total number of matching documents (may be more than returned)."""

    query: str
    """The original query string."""

    search_time_ms: float
    """Time taken for the search in milliseconds."""


class TypeSenseClient(ABC):
    """
    Abstract interface for TypeSense search operations.

    This interface allows different implementations:
    - TypeSenseStub: Returns empty results (for testing/development)
    - TypeSenseLocal: Connects to a local TypeSense instance
    - TypeSenseCloud: Connects to TypeSense Cloud
    """

    @abstractmethod
    def initialize_collection(
        self,
        collection_name: str,
        schema: dict[str, Any],
        documents_path: Path | None = None,
    ) -> None:
        """
        Initialize a TypeSense collection.

        Args:
            collection_name: Name of the collection to create/update.
            schema: TypeSense schema definition for the collection.
            documents_path: Optional path to a directory containing JSON documents
                           to index into the collection.
        """
        pass

    @abstractmethod
    def search(
        self,
        collection_name: str,
        query: str,
        query_by: list[str],
        filter_by: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResponse:
        """
        Search documents in a collection.

        Args:
            collection_name: Name of the collection to search.
            query: The search query string.
            query_by: List of fields to search in.
            filter_by: Optional filter expression (TypeSense syntax).
            limit: Maximum number of results to return.
            offset: Number of results to skip (for pagination).

        Returns:
            SearchResponse containing matching documents.
        """
        pass

    @abstractmethod
    def index_document(
        self,
        collection_name: str,
        document: dict[str, Any],
    ) -> str:
        """
        Index a single document into a collection.

        Args:
            collection_name: Name of the collection.
            document: The document to index.

        Returns:
            The document ID.
        """
        pass

    @abstractmethod
    def delete_collection(self, collection_name: str) -> None:
        """
        Delete a collection and all its documents.

        Args:
            collection_name: Name of the collection to delete.
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Close the client and release resources."""
        pass


class TypeSenseStub(TypeSenseClient):
    """
    Stub implementation of TypeSense that returns empty results.

    This is used when TypeSense is not available or not needed.
    All search operations return empty results with a warning log.

    TODO: Replace with actual TypeSense implementation when the
    typesense-python package is integrated.
    """

    def __init__(self):
        """Initialize the stub client."""
        self._collections: dict[str, dict[str, Any]] = {}
        self._documents: dict[str, list[dict[str, Any]]] = {}
        logger.warning(
            "TypeSenseStub initialized - search operations will return empty results. "
            "TODO: Implement actual TypeSense integration."
        )

    def initialize_collection(
        self,
        collection_name: str,
        schema: dict[str, Any],
        documents_path: Path | None = None,
    ) -> None:
        """
        Initialize a collection (stub - stores schema only).

        Args:
            collection_name: Name of the collection.
            schema: TypeSense schema definition.
            documents_path: Path to documents directory (ignored in stub).
        """
        self._collections[collection_name] = schema
        self._documents[collection_name] = []

        if documents_path:
            logger.info(
                f"TypeSenseStub: Would index documents from {documents_path} "
                f"into collection '{collection_name}' (stub - skipping)"
            )

    def search(
        self,
        collection_name: str,
        query: str,
        query_by: list[str],
        filter_by: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResponse:
        """
        Search documents (stub - returns empty results).

        Args:
            collection_name: Name of the collection.
            query: The search query.
            query_by: Fields to search.
            filter_by: Optional filter (ignored).
            limit: Max results (ignored).
            offset: Pagination offset (ignored).

        Returns:
            Empty SearchResponse.
        """
        logger.warning(
            f"TypeSenseStub: Search in '{collection_name}' for '{query}' "
            f"returned empty results (stub implementation)"
        )
        return SearchResponse(
            hits=[],
            total_hits=0,
            query=query,
            search_time_ms=0.0,
        )

    def index_document(
        self,
        collection_name: str,
        document: dict[str, Any],
    ) -> str:
        """
        Index a document (stub - stores in memory).

        Args:
            collection_name: Name of the collection.
            document: The document to index.

        Returns:
            A generated document ID.
        """
        if collection_name not in self._documents:
            self._documents[collection_name] = []

        doc_id = f"stub-{len(self._documents[collection_name])}"
        document["id"] = doc_id
        self._documents[collection_name].append(document)
        return doc_id

    def delete_collection(self, collection_name: str) -> None:
        """
        Delete a collection (stub - removes from memory).

        Args:
            collection_name: Name of the collection.
        """
        self._collections.pop(collection_name, None)
        self._documents.pop(collection_name, None)

    def close(self) -> None:
        """Close the stub client (no-op)."""
        pass


def create_typesense_client(
    host: str | None = None,
    port: int | None = None,
    api_key: str | None = None,
    use_stub: bool = True,
) -> TypeSenseClient:
    """
    Factory function to create a TypeSense client.

    Args:
        host: TypeSense server host (ignored if use_stub=True).
        port: TypeSense server port (ignored if use_stub=True).
        api_key: TypeSense API key (ignored if use_stub=True).
        use_stub: If True, return a stub client that returns empty results.

    Returns:
        A TypeSenseClient instance.

    TODO: Implement actual TypeSense client when use_stub=False.
    """
    if use_stub:
        return TypeSenseStub()

    # TODO: Implement actual TypeSense client
    # from typesense import Client
    # return TypeSenseLocalClient(host, port, api_key)
    raise NotImplementedError(
        "Actual TypeSense client not yet implemented. "
        "Use use_stub=True or implement TypeSenseLocal/TypeSenseCloud."
    )
