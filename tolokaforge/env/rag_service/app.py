"""
RAG Service - Hybrid BM25 + FAISS Search

This FastAPI service provides document indexing and hybrid search
for knowledge base queries. It supports:

- Per-trial document indexing
- BM25 (keyword) search via rank_bm25
- FAISS (semantic) search via sentence-transformers
- Hybrid search combining both methods

Usage:
    uvicorn app:app --host 0.0.0.0 --port 8001

API Endpoints:
    POST /trials/{trial_id}/index - Index documents for a trial
    POST /trials/{trial_id}/search - Search indexed documents
    DELETE /trials/{trial_id}/index - Delete trial's index
    GET /health - Health check
"""

import hashlib
import logging
import os
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Service version
SERVICE_VERSION = "1.0.0"

# Try to import sentence-transformers for FAISS search
try:
    from sentence_transformers import SentenceTransformer

    FAISS_AVAILABLE = True
    logger.info("sentence-transformers available, FAISS search enabled")
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("sentence-transformers not available, using BM25 only")

# Try to import FAISS
try:
    import faiss

    logger.info("FAISS available")
except ImportError:
    faiss = None
    logger.warning("FAISS not available, using numpy for similarity search")


# =============================================================================
# Pydantic Models
# =============================================================================


class Document(BaseModel):
    """A document to be indexed."""

    doc_id: str
    text: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    """Request to index documents."""

    trial_id: str
    domain_name: str
    documents: list[Document]


class IndexResponse(BaseModel):
    """Response from indexing."""

    status: str
    trial_id: str
    domain_name: str
    documents_indexed: int
    index_id: str


class SearchRequest(BaseModel):
    """Request to search documents."""

    query: str
    top_k: int = 5
    alpha: float = 0.5  # 0.0=BM25 only, 1.0=FAISS only


class SearchResult(BaseModel):
    """A single search result."""

    doc_id: str
    text: str
    source: str
    score: float
    retrieval_method: str


class SearchResponse(BaseModel):
    """Response from search."""

    results: list[SearchResult]
    query: str
    trial_id: str
    total_results: int


class DeleteResponse(BaseModel):
    """Response from delete."""

    status: str
    trial_id: str
    deleted: bool


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    active_indices: int
    faiss_available: bool


# =============================================================================
# Index Storage
# =============================================================================


class TrialIndex:
    """Index for a single trial's documents."""

    def __init__(
        self,
        trial_id: str,
        domain_name: str,
        documents: list[Document],
        embedding_model: Any | None = None,
    ):
        self.trial_id = trial_id
        self.domain_name = domain_name
        self.documents = documents
        self.embedding_model = embedding_model

        # Generate index ID from content hash
        content_hash = hashlib.sha256("".join(d.text for d in documents).encode()).hexdigest()[:16]
        self.index_id = f"{trial_id}_{content_hash}"

        # Build BM25 index
        self._build_bm25_index()

        # Build FAISS index if available
        self._embeddings: np.ndarray | None = None
        self._faiss_index: Any | None = None
        if embedding_model is not None:
            self._build_faiss_index()

    def _build_bm25_index(self) -> None:
        """Build BM25 index from documents."""
        # Tokenize documents (simple whitespace tokenization)
        tokenized_docs = [doc.text.lower().split() for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_docs)
        logger.info(f"Built BM25 index for {self.trial_id} with {len(self.documents)} docs")

    def _build_faiss_index(self) -> None:
        """Build FAISS index from document embeddings."""
        if self.embedding_model is None:
            return

        # Generate embeddings
        texts = [doc.text for doc in self.documents]
        embeddings = self.embedding_model.encode(texts, convert_to_numpy=True)
        self._embeddings = embeddings

        # Build FAISS index
        if faiss is not None and embeddings is not None:
            dimension = embeddings.shape[1]
            self._faiss_index = faiss.IndexFlatIP(dimension)  # Inner product (cosine sim)
            # Normalize embeddings for cosine similarity
            faiss.normalize_L2(embeddings)
            self._faiss_index.add(embeddings)
            logger.info(f"Built FAISS index for {self.trial_id}")
        elif embeddings is not None:
            # Normalize for numpy cosine similarity
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            self._embeddings = embeddings / norms
            logger.info(f"Built numpy similarity index for {self.trial_id}")

    def search_bm25(self, query: str, top_k: int = 5) -> list[tuple]:
        """
        Search using BM25.

        Returns:
            List of (doc_index, score) tuples
        """
        tokenized_query = query.lower().split()
        scores = self.bm25.get_scores(tokenized_query)

        # Get top-k indices
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [(int(idx), float(scores[idx])) for idx in top_indices if scores[idx] > 0]

    def search_faiss(self, query: str, top_k: int = 5) -> list[tuple]:
        """
        Search using FAISS/embeddings.

        Returns:
            List of (doc_index, score) tuples
        """
        if self._embeddings is None or self.embedding_model is None:
            return []

        # Encode query
        query_embedding = self.embedding_model.encode([query], convert_to_numpy=True)

        if faiss is not None and self._faiss_index is not None:
            # Normalize query for cosine similarity
            faiss.normalize_L2(query_embedding)
            scores, indices = self._faiss_index.search(query_embedding, top_k)
            return [
                (int(idx), float(score)) for idx, score in zip(indices[0], scores[0]) if idx >= 0
            ]
        else:
            # Numpy cosine similarity
            query_norm = query_embedding / np.linalg.norm(query_embedding)
            similarities = np.dot(self._embeddings, query_norm.T).flatten()
            top_indices = np.argsort(similarities)[::-1][:top_k]
            return [(int(idx), float(similarities[idx])) for idx in top_indices]

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        alpha: float = 0.5,
    ) -> list[SearchResult]:
        """
        Hybrid search combining BM25 and FAISS.

        Args:
            query: Search query
            top_k: Number of results
            alpha: Weight for FAISS (0.0=BM25 only, 1.0=FAISS only)

        Returns:
            List of SearchResult objects
        """
        # Get BM25 results
        bm25_results = self.search_bm25(query, top_k * 2)  # Get more for merging

        # Get FAISS results if available
        faiss_results = []
        if self._embeddings is not None and alpha > 0:
            faiss_results = self.search_faiss(query, top_k * 2)

        # Determine retrieval method
        if alpha == 0.0 or not faiss_results:
            retrieval_method = "bm25"
        elif alpha == 1.0 or not bm25_results:
            retrieval_method = "faiss"
        else:
            retrieval_method = "hybrid"

        # Normalize scores
        def normalize_scores(results: list[tuple]) -> dict[int, float]:
            if not results:
                return {}
            max_score = max(score for _, score in results) or 1.0
            return {idx: score / max_score for idx, score in results}

        bm25_scores = normalize_scores(bm25_results)
        faiss_scores = normalize_scores(faiss_results)

        # Combine scores
        all_indices = set(bm25_scores.keys()) | set(faiss_scores.keys())
        combined_scores = {}

        for idx in all_indices:
            bm25_score = bm25_scores.get(idx, 0.0)
            faiss_score = faiss_scores.get(idx, 0.0)
            combined_scores[idx] = (1 - alpha) * bm25_score + alpha * faiss_score

        # Sort by combined score
        sorted_indices = sorted(
            combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True
        )

        # Build results
        results = []
        for idx in sorted_indices[:top_k]:
            doc = self.documents[idx]
            results.append(
                SearchResult(
                    doc_id=doc.doc_id,
                    text=doc.text,
                    source=doc.source,
                    score=combined_scores[idx],
                    retrieval_method=retrieval_method,
                )
            )

        return results


# =============================================================================
# Global State
# =============================================================================


class RAGServiceState:
    """Global state for the RAG service."""

    def __init__(self):
        self.indices: dict[str, TrialIndex] = {}
        self.embedding_model: Any | None = None

        # Load embedding model if available
        if FAISS_AVAILABLE:
            model_name = os.environ.get(
                "EMBEDDING_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            )
            try:
                logger.info(f"Loading embedding model: {model_name}")
                self.embedding_model = SentenceTransformer(model_name)
                logger.info("Embedding model loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                self.embedding_model = None

    def create_index(
        self,
        trial_id: str,
        domain_name: str,
        documents: list[Document],
    ) -> TrialIndex:
        """Create or replace index for a trial."""
        index = TrialIndex(
            trial_id=trial_id,
            domain_name=domain_name,
            documents=documents,
            embedding_model=self.embedding_model,
        )
        self.indices[trial_id] = index
        return index

    def get_index(self, trial_id: str) -> TrialIndex | None:
        """Get index for a trial."""
        return self.indices.get(trial_id)

    def delete_index(self, trial_id: str) -> bool:
        """Delete index for a trial."""
        if trial_id in self.indices:
            del self.indices[trial_id]
            return True
        return False


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="RAG Service",
    description="Hybrid BM25 + FAISS search for knowledge base queries",
    version=SERVICE_VERSION,
)

# Global state
state = RAGServiceState()


@app.post("/trials/{trial_id}/index", response_model=IndexResponse)
async def index_documents(trial_id: str, request: IndexRequest) -> IndexResponse:
    """
    Index documents for a trial.

    Creates or replaces the search index for the given trial.
    """
    logger.info(f"Indexing {len(request.documents)} documents for trial {trial_id}")

    if not request.documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    try:
        index = state.create_index(
            trial_id=trial_id,
            domain_name=request.domain_name,
            documents=request.documents,
        )

        return IndexResponse(
            status="success",
            trial_id=trial_id,
            domain_name=request.domain_name,
            documents_indexed=len(request.documents),
            index_id=index.index_id,
        )

    except Exception as e:
        logger.error(f"Indexing failed for trial {trial_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Indexing failed: {str(e)}")


@app.post("/trials/{trial_id}/search", response_model=SearchResponse)
async def search_documents(trial_id: str, request: SearchRequest) -> SearchResponse:
    """
    Search indexed documents for a trial.

    Performs hybrid search combining BM25 and FAISS.
    """
    logger.debug(f"Searching trial {trial_id}: {request.query[:50]}...")

    index = state.get_index(trial_id)
    if index is None:
        raise HTTPException(
            status_code=404,
            detail=f"Index for trial '{trial_id}' not found",
        )

    try:
        results = index.search_hybrid(
            query=request.query,
            top_k=request.top_k,
            alpha=request.alpha,
        )

        return SearchResponse(
            results=results,
            query=request.query,
            trial_id=trial_id,
            total_results=len(results),
        )

    except Exception as e:
        logger.error(f"Search failed for trial {trial_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.delete("/trials/{trial_id}/index", response_model=DeleteResponse)
async def delete_index(trial_id: str) -> DeleteResponse:
    """
    Delete the search index for a trial.
    """
    logger.info(f"Deleting index for trial {trial_id}")

    deleted = state.delete_index(trial_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Index for trial '{trial_id}' not found",
        )

    return DeleteResponse(
        status="success",
        trial_id=trial_id,
        deleted=True,
    )


# Legacy endpoint for compatibility with existing SearchKBTool
@app.post("/search")
async def search_legacy(request: SearchRequest) -> list[SearchResult]:
    """
    Legacy search endpoint (no trial isolation).

    Uses a default "global" trial for backward compatibility.
    """
    # Use global index if exists, otherwise return empty
    index = state.get_index("global")
    if index is None:
        return []

    return index.search_hybrid(
        query=request.query,
        top_k=request.top_k,
        alpha=request.alpha,
    )


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Health check endpoint.
    """
    return HealthResponse(
        status="healthy",
        version=SERVICE_VERSION,
        active_indices=len(state.indices),
        faiss_available=FAISS_AVAILABLE and state.embedding_model is not None,
    )


# =============================================================================
# Startup/Shutdown
# =============================================================================


@app.on_event("startup")
async def startup_event():
    """Initialize service on startup."""
    logger.info(f"RAG Service v{SERVICE_VERSION} starting")
    logger.info(f"FAISS available: {FAISS_AVAILABLE}")
    logger.info(f"Embedding model loaded: {state.embedding_model is not None}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("RAG Service shutting down")
    state.indices.clear()
