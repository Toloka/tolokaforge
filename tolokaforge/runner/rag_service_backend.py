"""The rag-service search backend, registered as ``rag_service`` (ADR-0053).

The engine's built-in retrieval: the hybrid BM25 + dense rag-service
(``tolokaforge/env/rag_service``), one index per trial. It resolves through the
``tolokaforge.search_backends`` group like any third-party backend and is what a
task gets when ``initial_state.rag.backend`` is left at its default.

Its behaviour:

* :meth:`RagServiceBackend.build_index` indexes the corpus into rag-service
  through the runner's handle on it, refusing a corpus that loads no documents;
* :meth:`RagServiceSearchIndex.search` answers the agent's call with this
  JSON — ``top_k`` 5 and ``alpha`` 0.5 unless the call names them, ``limit`` read
  as an alias of ``top_k``, ``{"error": "Query is required", "results": []}``
  for an empty query — and lets a rag-service failure propagate;
* :meth:`RagServiceSearchIndex.knowledge_search` gives the judge a
  :class:`~tolokaforge.core.grading.kb_search.RagServiceKnowledgeSearch` bound to
  the same client and trial, so it searches the index the agent searched.

It declares ``stack_service = RAG_SERVICE_STACK_SERVICE``: the orchestrator starts
``full_stack`` for its tasks, and the runner builds its index only when it reaches
rag-service, whose handle (:class:`~tolokaforge.core.search.stack_services.RagServiceHandle`)
it reads with ``context.stack_services.get(RAG_SERVICE)``. It takes no
``backend_config``.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch, SearchHit
from tolokaforge.core.search.backend import (
    SearchBackendContext,
    SearchIndexBuildError,
    SearchOutcome,
)
from tolokaforge.core.search.stack_services import (
    RAG_SERVICE,
    RAG_SERVICE_STACK_SERVICE,
    RagServiceHandle,
    StackServiceUnavailableError,
)
from tolokaforge.runner.models import SearchPlane
from tolokaforge.runner.rag_client import (
    RAGServiceError,
    SearchResponse,
    load_documents_from_directory,
)

__all__ = [
    "RagServiceBackend",
    "RagServiceSearchIndex",
]

_TOOL_PARAMETERS: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query to find relevant documents",
        },
        "top_k": {
            "type": "integer",
            "description": "Number of results to return (default: 5)",
            "default": 5,
        },
        "alpha": {
            "type": "number",
            "description": "Weight for hybrid search: 0.0=keyword only, 1.0=semantic only, 0.5=balanced (default: 0.5)",
            "default": 0.5,
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_DEFAULT_TOP_K = 5
_DEFAULT_ALPHA = 0.5


class RagServiceSearchIndex:
    """One trial's rag-service index, searched over the runner's handle on rag-service."""

    def __init__(
        self, *, client: RagServiceHandle, trial_id: str, context: SearchBackendContext
    ) -> None:
        self._client = client
        self._trial_id = trial_id
        self._tool_name = context.tool_name
        self._logger = context.logger

    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome:
        """Search the trial's index; a rag-service failure raises (fail fast)."""
        start_time = time.perf_counter()
        self._logger.debug(
            f"rag_service search ENTRY: tool={self._tool_name}, arguments={arguments}"
        )
        if not query:
            self._log_exit(start_time, success=True)
            return SearchOutcome(
                hits=(), rendered=json.dumps({"error": "Query is required", "results": []})
            )

        top_k = arguments.get("top_k", arguments.get("limit", _DEFAULT_TOP_K))
        alpha = arguments.get("alpha", _DEFAULT_ALPHA)
        self._logger.debug(
            f"RAG search: trial={self._trial_id}, query={query[:50]}..., top_k={top_k}"
        )
        try:
            response: SearchResponse = await self._client.search(
                trial_id=self._trial_id,
                query=query,
                limit=top_k,
                alpha=alpha,
                timeout=budget_s,
            )
        except RAGServiceError as e:
            self._log_exit(start_time, success=False)
            self._logger.error(f"RAG search failed: {e}")
            raise

        outcome = SearchOutcome(
            hits=tuple(
                SearchHit(doc_id=hit.doc_id, source=hit.source, score=hit.score, text=hit.text)
                for hit in response.results
            ),
            rendered=_render(response, query),
        )
        self._log_exit(start_time, success=True)
        return outcome

    def knowledge_search(self) -> RagServiceKnowledgeSearch:
        """The judge's search over this trial's index, through the same handle."""
        return RagServiceKnowledgeSearch(self._client, self._trial_id)

    def _log_exit(self, start_time: float, *, success: bool) -> None:
        latency_ms = (time.perf_counter() - start_time) * 1000
        self._logger.debug(
            f"rag_service search EXIT: tool={self._tool_name}, "
            f"success={success}, state_changed=False, latency_ms={latency_ms:.2f}"
        )


def _render(response: SearchResponse, query: str) -> str:
    """The JSON the agent reads for one rag-service answer."""
    if not response.results:
        return json.dumps(
            {"message": "No relevant documents found.", "results": [], "query": query}
        )
    results = [
        {
            "doc_id": result.doc_id,
            "source": result.source,
            "score": result.score,
            "text": result.text,
            "retrieval_method": result.retrieval_method,
        }
        for result in response.results
    ]
    return json.dumps({"results": results, "total": len(results), "query": query})


class RagServiceBackend:
    """The ``rag_service`` :class:`~tolokaforge.core.search.backend.SearchBackend`."""

    name = SearchPlane.RAG_SERVICE.value
    stack_service: str | None = RAG_SERVICE_STACK_SERVICE

    def __init__(self, context: SearchBackendContext) -> None:
        if context.backend_config:
            raise ValueError(
                f"search backend {self.name!r} takes no backend_config, got keys "
                f"{sorted(context.backend_config)}; initial_state.rag.backend_config is read "
                "only by a backend that declares its own configuration"
            )
        self._context = context

    def tool_parameters(self) -> Mapping[str, Any]:
        """``query`` plus the hybrid's ``top_k`` and ``alpha``; a fresh copy per call."""
        return copy.deepcopy(dict(_TOOL_PARAMETERS))

    async def build_index(self, corpus_dir: Path | None) -> RagServiceSearchIndex:
        """Index the trial's corpus into rag-service (FAIL FAST).

        Raises:
            SearchIndexBuildError: the context holds no rag-service handle (the
                runner refuses such a trial before it builds, so this reaches a
                caller that builds outside the runner), or indexing failed, which
                includes a corpus that is unset or loads no documents: a declared
                corpus that indexes empty is a bundling bug, not an agent failure.
        """
        trial_id = self._context.trial_id
        if trial_id is None:
            raise RuntimeError(
                f"search backend {self.name!r} was asked to build an index from a trial-less "
                "context; only the runner builds one, at RegisterTrial"
            )
        try:
            client = self._context.stack_services.get(RAG_SERVICE)
        except StackServiceUnavailableError as e:
            raise SearchIndexBuildError(f"Trial {trial_id}: {e}") from e
        try:
            await self._index_corpus(client, trial_id, corpus_dir)
        except RAGServiceError as e:
            raise SearchIndexBuildError(f"RAG indexing failed: {e}") from e
        return RagServiceSearchIndex(client=client, trial_id=trial_id, context=self._context)

    async def _index_corpus(
        self, client: RagServiceHandle, trial_id: str, corpus_dir: Path | None
    ) -> None:
        if corpus_dir is None:
            raise RAGServiceError(
                f"Trial {trial_id}: search is enabled but documents_path is unset"
            )
        domain_name = self._context.domain_name or "default"
        documents = load_documents_from_directory(str(corpus_dir), domain_name)
        if not documents:
            raise RAGServiceError(
                f"Trial {trial_id}: no documents in resolved corpus {corpus_dir} "
                f"— corpus bundling is broken"
            )
        self._context.logger.info(
            f"Indexing {len(documents)} documents for trial {trial_id}",
            extra={
                "trial_id": trial_id,
                "domain_name": domain_name,
                "documents_path": str(corpus_dir),
            },
        )
        await client.index_documents(
            trial_id=trial_id,
            domain_name=domain_name,
            documents=documents,
        )


def _rag_service_backend_factory(context: SearchBackendContext) -> RagServiceBackend:
    """The ``tolokaforge.search_backends`` entry point for ``rag_service``."""
    return RagServiceBackend(context)
