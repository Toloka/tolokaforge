"""The ``rag_service`` search backend keeps the behaviour the runner had before the seam.

The expectations are literals on purpose: they are what the agent read, the request
rag-service received and the refusals ``RegisterTrial`` returned before ADR-0052
moved the code here, and a backend that drifted from any of them would change a
default every rag task runs under.

A recording client stands in for rag-service, so the backend's own work — what it
indexes, what it asks for, what it renders, what it refuses — is what is asserted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch, SearchHit
from tolokaforge.core.plugin_registry import available_search_backends, load_search_backend
from tolokaforge.core.search.backend import (
    RAG_SERVICE_STACK_SERVICE,
    SearchBackend,
    SearchBackendContext,
    SearchIndex,
    SearchIndexBuildError,
    SearchOutcome,
)
from tolokaforge.core.search.stack_services import StackServices
from tolokaforge.runner.rag_client import (
    RAGServiceClient,
    RAGServiceError,
    SearchResponse,
    SearchResult,
)
from tolokaforge.runner.rag_service_backend import RagServiceBackend, RagServiceSearchIndex

pytestmark = pytest.mark.unit

TRIAL_ID = "kb_lookup_01:0"


class _RecordingRagClient(RAGServiceClient):
    """The runner's client with the network replaced by a script and a record."""

    def __init__(self, answers: list[Any] | None = None) -> None:
        super().__init__(base_url="http://rag-service:8001/", timeout=12.5)
        self.indexed: list[tuple[str, str, list]] = []
        self.searches: list[dict[str, Any]] = []
        self._answers = list(answers or [])

    async def index_documents(self, trial_id: str, domain_name: str, documents: list) -> Any:
        self.indexed.append((trial_id, domain_name, documents))

    async def search(  # type: ignore[override]
        self, trial_id: str, query: str, limit: int = 5, alpha: float = 0.5, timeout=None
    ) -> SearchResponse:
        self.searches.append(
            {
                "trial_id": trial_id,
                "query": query,
                "limit": limit,
                "alpha": alpha,
                "timeout": timeout,
            }
        )
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SearchResponse(
            results=answer, query=query, trial_id=trial_id, total_results=len(answer)
        )


def _context(
    client: RAGServiceClient | None = None,
    *,
    trial_id: str | None = TRIAL_ID,
    domain_name: str | None = "rag_search",
    backend_config: dict[str, Any] | None = None,
) -> SearchBackendContext:
    return SearchBackendContext(
        backend_config=backend_config or {},
        tool_name="search_kb",
        tool_description="Search the knowledge base.",
        logger=logging.getLogger("test.rag_service"),
        trial_id=trial_id,
        domain_name=domain_name,
        stack_services=StackServices(rag_service=client),
    )


def _corpus(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "policies.md").write_text("# Policies\n\nRefund code RX-7788.\n")
    (directory / "faq.txt").write_text("Q: return window?\n")
    return directory


def _index(client: _RecordingRagClient, tmp_path: Path) -> RagServiceSearchIndex:
    backend = RagServiceBackend(_context(client))
    return asyncio.run(backend.build_index(_corpus(tmp_path / "corpus")))


def _hit(doc_id: str, score: float) -> SearchResult:
    return SearchResult(
        doc_id=doc_id,
        text=f"text of {doc_id}",
        source=f"{doc_id}.md",
        score=score,
        retrieval_method="hybrid",
    )


def _search(index: SearchIndex, arguments: dict[str, Any], budget_s: float = 15.0) -> SearchOutcome:
    return asyncio.run(index.search(arguments.get("query", ""), arguments, budget_s=budget_s))


class TestRegistration:
    def test_rag_service_is_registered_and_builds_a_search_backend(self) -> None:
        assert "rag_service" in available_search_backends()
        backend = load_search_backend("rag_service")(_context())
        assert isinstance(backend, RagServiceBackend)
        assert isinstance(backend, SearchBackend)
        assert backend.name == "rag_service"
        assert backend.stack_service == RAG_SERVICE_STACK_SERVICE == "rag_service"

    def test_the_factory_needs_no_trial(self) -> None:
        """The adapter and the stack rule build it orchestrator-side to read its declaration."""
        backend = load_search_backend("rag_service")(_context(trial_id=None, domain_name=None))
        assert backend.tool_parameters() is not None

    def test_a_backend_config_is_refused(self) -> None:
        with pytest.raises(ValueError, match="takes no backend_config"):
            RagServiceBackend(_context(backend_config={"top_k": 3}))


def test_the_agent_sees_query_top_k_and_alpha() -> None:
    parameters = RagServiceBackend(_context()).tool_parameters()
    assert json.dumps(parameters) == json.dumps(
        {
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
    )


def test_tool_parameters_hands_out_a_copy() -> None:
    backend = RagServiceBackend(_context())
    backend.tool_parameters()["properties"]["query"]["type"] = "integer"
    assert backend.tool_parameters()["properties"]["query"]["type"] == "string"


class TestBuildIndex:
    def test_the_corpus_is_indexed_through_the_runners_client(self, tmp_path: Path) -> None:
        client = _RecordingRagClient()
        index = _index(client, tmp_path)

        assert isinstance(index, SearchIndex)
        trial_id, domain_name, documents = client.indexed[0]
        assert (trial_id, domain_name) == (TRIAL_ID, "rag_search")
        assert sorted(doc.source for doc in documents) == ["faq.txt", "policies.md"]

    def test_an_undeclared_domain_indexes_as_default(self, tmp_path: Path) -> None:
        client = _RecordingRagClient()
        backend = RagServiceBackend(_context(client, domain_name=None))
        asyncio.run(backend.build_index(_corpus(tmp_path / "corpus")))
        assert client.indexed[0][1] == "default"

    def test_a_context_without_a_rag_service_handle_refuses_the_trial(self, tmp_path: Path) -> None:
        backend = RagServiceBackend(_context(None))
        with pytest.raises(SearchIndexBuildError) as excinfo:
            asyncio.run(backend.build_index(_corpus(tmp_path / "corpus")))
        message = str(excinfo.value)
        assert message.startswith(f"Trial {TRIAL_ID}: stack service 'rag_service' is not reachable")
        assert "RAG_SERVICE_URL" in message, "the refusal says how a runner reaches rag-service"

    def test_an_unset_corpus_is_refused(self) -> None:
        backend = RagServiceBackend(_context(_RecordingRagClient()))
        with pytest.raises(SearchIndexBuildError) as excinfo:
            asyncio.run(backend.build_index(None))
        assert str(excinfo.value) == (
            f"RAG indexing failed: Trial {TRIAL_ID}: search is enabled but documents_path is unset"
        )

    @pytest.mark.parametrize("make_dir", [False, True], ids=["missing-dir", "empty-dir"])
    def test_a_corpus_with_no_documents_is_refused(self, tmp_path: Path, make_dir: bool) -> None:
        client = _RecordingRagClient()
        corpus = tmp_path / "corpus"
        if make_dir:
            corpus.mkdir()
        with pytest.raises(SearchIndexBuildError, match="^RAG indexing failed: .*no documents"):
            asyncio.run(RagServiceBackend(_context(client)).build_index(corpus))
        assert client.indexed == []

    def test_an_indexing_failure_is_refused_verbatim(self, tmp_path: Path) -> None:
        class _Failing(_RecordingRagClient):
            async def index_documents(self, trial_id, domain_name, documents):
                raise RAGServiceError("Indexing failed: disk full", status_code=500)

        with pytest.raises(SearchIndexBuildError) as excinfo:
            asyncio.run(
                RagServiceBackend(_context(_Failing())).build_index(_corpus(tmp_path / "c"))
            )
        assert str(excinfo.value) == "RAG indexing failed: Indexing failed: disk full"

    def test_a_trial_less_context_builds_no_index(self, tmp_path: Path) -> None:
        backend = RagServiceBackend(_context(_RecordingRagClient(), trial_id=None))
        with pytest.raises(RuntimeError, match="trial-less"):
            asyncio.run(backend.build_index(_corpus(tmp_path / "corpus")))


class TestSearch:
    def test_hits_render_as_the_json_the_agent_read(self, tmp_path: Path) -> None:
        client = _RecordingRagClient([[_hit("d1", 0.9), _hit("d2", 0.25)]])
        outcome = _search(_index(client, tmp_path), {"query": "refund code"})

        assert outcome.rendered == (
            '{"results": [{"doc_id": "d1", "source": "d1.md", "score": 0.9, '
            '"text": "text of d1", "retrieval_method": "hybrid"}, {"doc_id": "d2", '
            '"source": "d2.md", "score": 0.25, "text": "text of d2", '
            '"retrieval_method": "hybrid"}], "total": 2, "query": "refund code"}'
        )
        assert outcome.hits == (
            SearchHit(doc_id="d1", source="d1.md", score=0.9, text="text of d1"),
            SearchHit(doc_id="d2", source="d2.md", score=0.25, text="text of d2"),
        )

    def test_no_hits_render_the_no_documents_message(self, tmp_path: Path) -> None:
        client = _RecordingRagClient([[]])
        outcome = _search(_index(client, tmp_path), {"query": "nothing"})
        assert outcome.rendered == (
            '{"message": "No relevant documents found.", "results": [], "query": "nothing"}'
        )
        assert outcome.hits == ()

    @pytest.mark.parametrize("arguments", [{"query": ""}, {}], ids=["empty", "absent"])
    def test_an_empty_query_is_answered_without_a_request(
        self, tmp_path: Path, arguments: dict[str, Any]
    ) -> None:
        client = _RecordingRagClient()
        outcome = _search(_index(client, tmp_path), arguments)
        assert outcome.rendered == '{"error": "Query is required", "results": []}'
        assert client.searches == []

    @pytest.mark.parametrize(
        ("arguments", "limit", "alpha"),
        [
            ({"query": "q"}, 5, 0.5),
            ({"query": "q", "top_k": 2, "alpha": 0.0}, 2, 0.0),
            ({"query": "q", "limit": 7}, 7, 0.5),
            ({"query": "q", "top_k": 3, "limit": 9}, 3, 0.5),
        ],
        ids=["defaults", "declared", "limit-alias", "top-k-wins"],
    )
    def test_the_request_carries_the_calls_parameters_and_the_budget(
        self, tmp_path: Path, arguments: dict[str, Any], limit: int, alpha: float
    ) -> None:
        client = _RecordingRagClient([[]])
        _search(_index(client, tmp_path), arguments, budget_s=42.0)
        assert client.searches == [
            {"trial_id": TRIAL_ID, "query": "q", "limit": limit, "alpha": alpha, "timeout": 42.0}
        ]

    def test_a_failed_search_raises_instead_of_rendering_empty_results(
        self, tmp_path: Path
    ) -> None:
        client = _RecordingRagClient([RAGServiceError("RAG service timeout: slow")])
        with pytest.raises(RAGServiceError, match="timeout"):
            _search(_index(client, tmp_path), {"query": "q"})


def test_the_judge_searches_the_same_trial_through_the_same_client(tmp_path: Path) -> None:
    client = _RecordingRagClient()
    knowledge_search = _index(client, tmp_path).knowledge_search()

    assert isinstance(knowledge_search, RagServiceKnowledgeSearch)
    assert knowledge_search._base_url == "http://rag-service:8001"
    assert knowledge_search._timeout == 12.5
    assert knowledge_search._trial_id == TRIAL_ID
