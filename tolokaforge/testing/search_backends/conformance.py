"""The conformance suite every search backend runs against itself.

:class:`~tolokaforge.core.search.backend.SearchBackend` is a small contract, but
every part of it is read somewhere other than where the backend lives: the native
adapter reads ``tool_parameters()`` off a backend it built with no trial, the
orchestrator reads ``stack_service`` to choose the stack, the runner hands the
``query`` argument to ``search`` by name and returns ``rendered`` to the agent, and
the judge reads ``knowledge_search()``. A break shows up as a wrong schema, a wrong
stack or a judge reading a different corpus than the agent — not as an exception
where the backend is.

Each test builds the backend under test through its factory and reads what a
conforming backend must produce, never the implementation. Adoption is the repo's
standard suite shape — subclass and supply the factory::

    from tolokaforge.testing.search_backends import SearchBackendConformanceSuite

    class TestMyBackendConformance(SearchBackendConformanceSuite):
        @pytest.fixture
        def backend_factory(self):
            return my_search_backend_factory

A backend that needs a stack service overrides ``trial_context`` too, handing it a
client under the name its ``stack_service`` declares. ``corpus_dir`` and ``query``
are overridable for a backend that reads other document shapes; the suite's
defaults are three Markdown documents and a query that one of them answers.

The base class carries no ``Test`` prefix so pytest does not collect it. Each test
takes its inputs as parameters, so the suite can also be driven directly — which is
how ``tests/canonical/test_search_backend_contract.py`` proves each assertion
fails on the backend that breaks it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit
from tolokaforge.core.search.backend import (
    SearchBackend,
    SearchBackendContext,
    SearchBackendFactory,
    SearchIndex,
    SearchIndexBuildError,
    SearchOutcome,
)

__all__ = [
    "CONFORMANCE_QUERY",
    "SearchBackendConformanceSuite",
    "declaration_context",
    "trial_context",
    "write_corpus",
]

CONFORMANCE_QUERY = "refund window"
_BUDGET_S = 15.0
_TRIAL_ID = "conformance:0"
_LOGGER = logging.getLogger("tolokaforge.testing.search_backends")

_CORPUS = {
    "returns.md": "# Returns\n\nThe refund window is thirty days from delivery.\n",
    "shipping.md": "# Shipping\n\nOrders ship within two business days.\n",
    "warranty.md": "# Warranty\n\nHardware carries a one year warranty.\n",
}


def write_corpus(directory: Path) -> Path:
    """The suite's default corpus: three Markdown documents, one answering the query."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in _CORPUS.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def declaration_context(**overrides: Any) -> SearchBackendContext:
    """The trial-less context the adapter and the stack rule build a backend from."""
    fields: dict[str, Any] = {
        "backend_config": {},
        "tool_name": "search_kb",
        "tool_description": "Search the knowledge base.",
        "logger": _LOGGER,
        **overrides,
    }
    return SearchBackendContext(**fields)


def trial_context(**overrides: Any) -> SearchBackendContext:
    """The context the runner builds at ``RegisterTrial``, holding no stack-service client."""
    fields: dict[str, Any] = {"trial_id": _TRIAL_ID, "domain_name": "conformance", **overrides}
    return declaration_context(**fields)


async def _search_both_sides(
    backend: SearchBackend, corpus_dir: Path, query: str
) -> tuple[SearchIndex, SearchOutcome, list[SearchHit] | None]:
    index = await backend.build_index(corpus_dir)
    outcome = await index.search(query, {"query": query}, budget_s=_BUDGET_S)
    knowledge_search = index.knowledge_search()
    judge_hits = (
        None
        if knowledge_search is None
        else knowledge_search.search(query, top_k=max(len(outcome.hits), 1))
    )
    return index, outcome, judge_hits


class SearchBackendConformanceSuite:
    """Subclass and override ``backend_factory`` to certify one search backend."""

    @pytest.fixture
    def backend_factory(self) -> SearchBackendFactory:
        raise NotImplementedError(
            "subclasses of SearchBackendConformanceSuite must override the "
            "`backend_factory` fixture to return a SearchBackendFactory — the same "
            "callable the `tolokaforge.search_backends` entry point resolves to"
        )

    @pytest.fixture(name="trial_context")
    def trial_context_fixture(self) -> SearchBackendContext:
        """The runner's context; override to hand the backend its stack-service client."""
        return trial_context()

    @pytest.fixture
    def corpus_dir(self, tmp_path: Path) -> Path:
        return write_corpus(tmp_path / "corpus")

    @pytest.fixture
    def query(self) -> str:
        return CONFORMANCE_QUERY

    def test_the_factory_builds_a_search_backend(
        self, backend_factory: SearchBackendFactory, trial_context: SearchBackendContext
    ) -> None:
        backend = backend_factory(trial_context)
        assert isinstance(backend, SearchBackend), (
            f"{type(backend).__name__} does not satisfy the SearchBackend Protocol; the "
            "runner resolves the factory and calls `build_index` on whatever it returns"
        )
        assert isinstance(backend.name, str) and backend.name, "a backend declares its name"
        assert backend.stack_service is None or isinstance(backend.stack_service, str), (
            f"stack_service is {type(backend.stack_service).__name__}; the stack rule "
            "compares it to the stack-service names it knows, so it is a name or None"
        )

    def test_the_factory_builds_from_a_trial_less_context(
        self, backend_factory: SearchBackendFactory
    ) -> None:
        """The adapter and the stack rule read the declaration before any trial exists."""
        try:
            backend = backend_factory(declaration_context())
        except Exception as exc:  # noqa: BLE001 — surfaced as a conformance failure
            raise AssertionError(
                "the factory refused a trial-less context; the native adapter builds the "
                "backend with no trial and no stack-service client to read the agent's "
                f"tool schema, and the stack rule to read stack_service: {exc}"
            ) from exc
        assert isinstance(backend, SearchBackend)

    def test_tool_parameters_is_a_parameters_object_with_a_query(
        self, backend_factory: SearchBackendFactory
    ) -> None:
        """The runner hands the agent's ``query`` argument to ``search`` by that name."""
        parameters = backend_factory(declaration_context()).tool_parameters()
        if parameters is None:
            return
        assert parameters.get("type") == "object", (
            "tool_parameters() is the JSON-schema `parameters` object the agent's tool "
            f"schema carries, so its type is 'object'; got {parameters.get('type')!r}"
        )
        properties = parameters.get("properties")
        assert isinstance(properties, dict) and "query" in properties, (
            "the parameters object declares no `query` property; the runner reads that "
            "argument off the agent's call and passes it to SearchIndex.search"
        )

    def test_search_returns_hits_and_the_rendered_text(
        self,
        backend_factory: SearchBackendFactory,
        trial_context: SearchBackendContext,
        corpus_dir: Path,
        query: str,
    ) -> None:
        """The agent reads ``rendered``; the judge, replay and the remote grader read hits."""
        index, outcome, _ = asyncio.run(
            _search_both_sides(backend_factory(trial_context), corpus_dir, query)
        )
        assert isinstance(index, SearchIndex), "build_index must return a SearchIndex"
        assert isinstance(outcome, SearchOutcome), (
            f"search returned {type(outcome).__name__}; the runner reads `.rendered` off a "
            "SearchOutcome"
        )
        assert isinstance(outcome.rendered, str), (
            f"rendered is {type(outcome.rendered).__name__}; it is the tool result the agent "
            "reads, so it is the text the backend formats"
        )
        assert isinstance(outcome.hits, tuple) and all(
            isinstance(hit, SearchHit) for hit in outcome.hits
        ), "hits is a tuple of SearchHit, the backend-neutral shape every reader consumes"
        assert outcome.hits, (
            "the suite's query found nothing in the suite's corpus; a backend that cannot "
            "retrieve a document naming the query's words cannot be certified here — "
            "override `corpus_dir` / `query` if it reads another document shape"
        )

    def test_the_judge_reads_the_index_the_agent_searched(
        self,
        backend_factory: SearchBackendFactory,
        trial_context: SearchBackendContext,
        corpus_dir: Path,
        query: str,
    ) -> None:
        """``knowledge_search()`` is the same index, or ``None`` for no judge search."""
        index, outcome, judge_hits = asyncio.run(
            _search_both_sides(backend_factory(trial_context), corpus_dir, query)
        )
        knowledge_search = index.knowledge_search()
        if knowledge_search is None:
            return
        assert isinstance(knowledge_search, KnowledgeSearch)
        assert judge_hits is not None
        agent_ids = [hit.doc_id for hit in outcome.hits]
        judge_ids = [hit.doc_id for hit in judge_hits][: len(agent_ids)]
        assert judge_ids == agent_ids, (
            "the judge's search returned other documents than the agent's for the same "
            f"query ({judge_ids} vs {agent_ids}); knowledge_search() must read the index "
            "the agent searched"
        )

    def test_a_corpus_with_no_documents_is_refused(
        self,
        backend_factory: SearchBackendFactory,
        trial_context: SearchBackendContext,
        tmp_path: Path,
    ) -> None:
        """A declared corpus that indexes empty is a bundling bug, not an agent failure."""
        empty = tmp_path / "empty_corpus"
        empty.mkdir(exist_ok=True)
        backend = backend_factory(trial_context)
        try:
            asyncio.run(backend.build_index(empty))
        except SearchIndexBuildError:
            return
        raise AssertionError(
            "build_index accepted a corpus with no documents; RegisterTrial must refuse the "
            "trial with a SearchIndexBuildError rather than let the agent search nothing"
        )
