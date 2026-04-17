"""
RAG Service HTTP Client

This module provides an async HTTP client for the RAG service.
The RAG service provides hybrid BM25 + FAISS search for knowledge base queries.

Usage:
    client = RAGServiceClient("http://rag-service:8001")
    await client.index_documents(trial_id, domain_name, documents)
    results = await client.search(trial_id, query, limit=5)
    await client.delete_index(trial_id)

FAIL FAST: All methods raise RAGServiceError on failures.
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Pydantic Models for RAG Service API
# =============================================================================


class Document(BaseModel):
    """A document to be indexed in the RAG service."""

    doc_id: str
    text: str
    source: str  # e.g., "policy.md", "faq.md"
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class IndexRequest(BaseModel):
    """Request to index documents for a trial."""

    trial_id: str
    domain_name: str
    documents: list[Document]

    model_config = {"extra": "forbid"}


class IndexResponse(BaseModel):
    """Response from indexing documents."""

    status: str
    trial_id: str
    domain_name: str
    documents_indexed: int
    index_id: str  # Unique identifier for this index

    model_config = {"extra": "allow"}


class SearchRequest(BaseModel):
    """Request to search indexed documents."""

    query: str
    top_k: int = 5
    alpha: float = 0.5  # 0.0=BM25 only, 1.0=FAISS only, 0.5=balanced

    model_config = {"extra": "forbid"}


class SearchResult(BaseModel):
    """A single search result."""

    doc_id: str
    text: str
    source: str
    score: float
    retrieval_method: str  # "bm25", "faiss", or "hybrid"

    model_config = {"extra": "allow"}


class SearchResponse(BaseModel):
    """Response from search query."""

    results: list[SearchResult]
    query: str
    trial_id: str
    total_results: int

    model_config = {"extra": "allow"}


class DeleteIndexResponse(BaseModel):
    """Response from deleting an index."""

    status: str
    trial_id: str
    deleted: bool

    model_config = {"extra": "allow"}


class HealthResponse(BaseModel):
    """Response from health check."""

    status: str
    version: str
    active_indices: int

    model_config = {"extra": "allow"}


# =============================================================================
# Custom Exceptions
# =============================================================================


class RAGServiceError(Exception):
    """Base exception for RAG service errors."""

    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RAGIndexNotFoundError(RAGServiceError):
    """Index for trial not found."""

    pass


class RAGIndexingError(RAGServiceError):
    """Error during document indexing."""

    pass


class RAGSearchError(RAGServiceError):
    """Error during search."""

    pass


class RAGConnectionError(RAGServiceError):
    """Cannot connect to RAG service."""

    pass


# =============================================================================
# RAG Service Client
# =============================================================================


class RAGServiceClient:
    """
    Async HTTP client for RAG service.

    The RAG service provides:
    - Document indexing with BM25 + FAISS
    - Hybrid search combining keyword and semantic search
    - Per-trial index isolation

    FAIL FAST: All methods raise RAGServiceError on failures.
    """

    def __init__(
        self,
        base_url: str = "http://rag-service:8001",
        timeout: float = 30.0,
    ):
        """
        Initialize RAG service client.

        Args:
            base_url: Base URL of the RAG service
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "RAGServiceClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    # =========================================================================
    # Index Management
    # =========================================================================

    async def index_documents(
        self,
        trial_id: str,
        domain_name: str,
        documents: list[Document],
    ) -> IndexResponse:
        """
        Index documents for a trial's search corpus.

        Creates or updates the search index for the given trial with the
        provided documents. Documents are indexed for both BM25 (keyword)
        and FAISS (semantic) search.

        Args:
            trial_id: Unique trial identifier
            domain_name: Domain name for the index (e.g., "external_retail_v3")
            documents: List of documents to index

        Returns:
            IndexResponse with indexing status

        Raises:
            RAGIndexingError: If indexing fails
            RAGConnectionError: If cannot connect to RAG service
        """
        logger.info(
            "Indexing documents",
            extra={
                "trial_id": trial_id,
                "domain_name": domain_name,
                "document_count": len(documents),
            },
        )

        try:
            client = await self._get_client()
            request = IndexRequest(
                trial_id=trial_id,
                domain_name=domain_name,
                documents=documents,
            )

            response = await client.post(
                f"/trials/{trial_id}/index",
                json=request.model_dump(),
            )

            if response.status_code == 200:
                data = response.json()
                result = IndexResponse.model_validate(data)
                logger.info(
                    "Documents indexed successfully",
                    extra={
                        "trial_id": trial_id,
                        "documents_indexed": result.documents_indexed,
                        "index_id": result.index_id,
                    },
                )
                return result
            else:
                error_detail = response.json().get("detail", response.text)
                raise RAGIndexingError(
                    f"Indexing failed: {error_detail}",
                    status_code=response.status_code,
                )

        except httpx.ConnectError as e:
            raise RAGConnectionError(f"Cannot connect to RAG service: {e}")
        except httpx.TimeoutException as e:
            raise RAGServiceError(f"RAG service timeout: {e}")
        except httpx.HTTPError as e:
            raise RAGServiceError(f"HTTP error: {e}")

    async def delete_index(self, trial_id: str) -> DeleteIndexResponse:
        """
        Delete the search index for a trial.

        Removes all indexed documents and frees resources for the trial.

        Args:
            trial_id: Unique trial identifier

        Returns:
            DeleteIndexResponse with deletion status

        Raises:
            RAGIndexNotFoundError: If index doesn't exist
            RAGConnectionError: If cannot connect to RAG service
        """
        logger.info("Deleting index", extra={"trial_id": trial_id})

        try:
            client = await self._get_client()
            response = await client.delete(f"/trials/{trial_id}/index")

            if response.status_code == 200:
                data = response.json()
                result = DeleteIndexResponse.model_validate(data)
                logger.info("Index deleted", extra={"trial_id": trial_id})
                return result
            elif response.status_code == 404:
                raise RAGIndexNotFoundError(
                    f"Index for trial '{trial_id}' not found",
                    status_code=404,
                )
            else:
                error_detail = response.json().get("detail", response.text)
                raise RAGServiceError(
                    f"Delete failed: {error_detail}",
                    status_code=response.status_code,
                )

        except httpx.ConnectError as e:
            raise RAGConnectionError(f"Cannot connect to RAG service: {e}")
        except httpx.TimeoutException as e:
            raise RAGServiceError(f"RAG service timeout: {e}")
        except httpx.HTTPError as e:
            raise RAGServiceError(f"HTTP error: {e}")

    # =========================================================================
    # Search
    # =========================================================================

    async def search(
        self,
        trial_id: str,
        query: str,
        limit: int = 5,
        alpha: float = 0.5,
    ) -> SearchResponse:
        """
        Search indexed documents.

        Performs hybrid search combining BM25 (keyword) and FAISS (semantic)
        search. The alpha parameter controls the balance:
        - alpha=0.0: BM25 only (pure keyword search)
        - alpha=1.0: FAISS only (pure semantic search)
        - alpha=0.5: Balanced hybrid (default)

        Args:
            trial_id: Unique trial identifier
            query: Search query string
            limit: Maximum number of results (default: 5)
            alpha: Hybrid search weight (default: 0.5)

        Returns:
            SearchResponse with search results

        Raises:
            RAGIndexNotFoundError: If index doesn't exist
            RAGSearchError: If search fails
            RAGConnectionError: If cannot connect to RAG service
        """
        logger.debug(
            "Searching documents",
            extra={
                "trial_id": trial_id,
                "query": query[:50] + "..." if len(query) > 50 else query,
                "limit": limit,
                "alpha": alpha,
            },
        )

        try:
            client = await self._get_client()
            request = SearchRequest(
                query=query,
                top_k=limit,
                alpha=alpha,
            )

            response = await client.post(
                f"/trials/{trial_id}/search",
                json=request.model_dump(),
            )

            if response.status_code == 200:
                data = response.json()
                # Handle both list response (legacy) and dict response
                if isinstance(data, list):
                    # Legacy format: list of results
                    results = [SearchResult.model_validate(r) for r in data]
                    result = SearchResponse(
                        results=results,
                        query=query,
                        trial_id=trial_id,
                        total_results=len(results),
                    )
                else:
                    result = SearchResponse.model_validate(data)

                logger.debug(
                    "Search completed",
                    extra={
                        "trial_id": trial_id,
                        "result_count": len(result.results),
                    },
                )
                return result
            elif response.status_code == 404:
                raise RAGIndexNotFoundError(
                    f"Index for trial '{trial_id}' not found",
                    status_code=404,
                )
            else:
                error_detail = response.json().get("detail", response.text)
                raise RAGSearchError(
                    f"Search failed: {error_detail}",
                    status_code=response.status_code,
                )

        except httpx.ConnectError as e:
            raise RAGConnectionError(f"Cannot connect to RAG service: {e}")
        except httpx.TimeoutException as e:
            raise RAGServiceError(f"RAG service timeout: {e}")
        except httpx.HTTPError as e:
            raise RAGServiceError(f"HTTP error: {e}")

    # =========================================================================
    # Health Check
    # =========================================================================

    async def health_check(self) -> HealthResponse:
        """
        Check RAG service health.

        Returns:
            HealthResponse with service status

        Raises:
            RAGConnectionError: If cannot connect to RAG service
        """
        try:
            client = await self._get_client()
            response = await client.get("/health")

            if response.status_code == 200:
                data = response.json()
                return HealthResponse.model_validate(data)
            else:
                raise RAGServiceError(
                    f"Health check failed: {response.text}",
                    status_code=response.status_code,
                )

        except httpx.ConnectError as e:
            raise RAGConnectionError(f"Cannot connect to RAG service: {e}")
        except httpx.TimeoutException as e:
            raise RAGServiceError(f"RAG service timeout: {e}")
        except httpx.HTTPError as e:
            raise RAGServiceError(f"HTTP error: {e}")

    async def is_healthy(self) -> bool:
        """
        Check if RAG service is healthy.

        Returns:
            True if service is healthy, False otherwise
        """
        try:
            health = await self.health_check()
            return health.status == "healthy"
        except RAGServiceError:
            return False


# =============================================================================
# Document Loading Utilities
# =============================================================================


def load_documents_from_directory(
    directory_path: str,
    domain_name: str,
) -> list[Document]:
    """
    Load documents from a directory (e.g., docindex/).

    Reads all .md and .txt files from the directory and creates
    Document objects for indexing.

    Args:
        directory_path: Path to directory containing documents
        domain_name: Domain name for document metadata

    Returns:
        List of Document objects
    """
    import hashlib
    from pathlib import Path

    documents = []
    doc_dir = Path(directory_path)

    if not doc_dir.exists():
        logger.warning(f"Document directory not found: {directory_path}")
        return documents

    # Load .md and .txt files
    for pattern in ["*.md", "*.txt"]:
        for file_path in doc_dir.glob(pattern):
            try:
                text = file_path.read_text(encoding="utf-8")
                if not text.strip():
                    continue

                # Generate doc_id from content hash
                doc_id = hashlib.sha256(text.encode()).hexdigest()[:16]

                documents.append(
                    Document(
                        doc_id=doc_id,
                        text=text,
                        source=file_path.name,
                        metadata={
                            "domain": domain_name,
                            "file_path": str(file_path),
                        },
                    )
                )
                logger.debug(f"Loaded document: {file_path.name}")

            except Exception as e:
                logger.error(f"Failed to load document {file_path}: {e}")

    logger.info(f"Loaded {len(documents)} documents from {directory_path}")
    return documents
