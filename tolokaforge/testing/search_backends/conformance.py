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
standard suite shape — subclass and supply two fixtures, the factory and how its
search fails::

    from tolokaforge.testing.search_backends import SearchBackendConformanceSuite

    class TestMyBackendConformance(SearchBackendConformanceSuite):
        @pytest.fixture
        def backend_factory(self):
            return my_search_backend_factory

        @pytest.fixture
        def make_searches_fail(self):
            return lambda index: my_service.go_down()

A backend that needs a stack service overrides ``trial_context`` too, handing it the
handle its ``stack_service`` declares in a
:class:`~tolokaforge.core.search.stack_services.StackServices`. ``corpus_dir`` and ``query``
are overridable for a backend that reads other document shapes; the suite's
defaults are three Markdown documents and a query that one of them answers.

The suite runs the backend the way the runner does (:class:`RunnerLoop`): the index
is built and the agent's search answered on one long-lived event loop on its own
thread, and the judge's synchronous ``KnowledgeSearch.search`` is called from
another thread while that loop keeps running.

The base class carries no ``Test`` prefix so pytest does not collect it. Each test
takes its inputs as parameters, so the suite can also be driven directly — which is
how ``tests/canonical/test_search_backend_contract.py`` proves each assertion
fails on the backend that breaks it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

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
from tolokaforge.core.search.stack_services import DECLARED_STACK_SERVICES

__all__ = [
    "CONFORMANCE_QUERY",
    "RunnerLoop",
    "SearchBackendConformanceSuite",
    "declaration_context",
    "trial_context",
    "write_corpus",
]

CONFORMANCE_QUERY = "refund window"
_BUDGET_S = 15.0
_CALL_TIMEOUT_S = 30.0
_TRIAL_ID = "conformance:0"
_LOGGER = logging.getLogger("tolokaforge.testing.search_backends")

_CORPUS = {
    "returns.md": "# Returns\n\nThe refund window is thirty days from delivery.\n",
    "shipping.md": "# Shipping\n\nOrders ship within two business days.\n",
    "warranty.md": "# Warranty\n\nHardware carries a one year warranty.\n",
}

_T = TypeVar("_T")


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
    """The context the runner builds at ``RegisterTrial``, holding no stack-service handle."""
    fields: dict[str, Any] = {"trial_id": _TRIAL_ID, "domain_name": "conformance", **overrides}
    return declaration_context(**fields)


class RunnerLoop:
    """The runner's threading shape: one long-lived event loop on a thread of its own.

    ``RegisterTrial`` awaits ``build_index`` and ``ExecuteTool`` awaits ``search`` on
    that loop, while the judge calls ``KnowledgeSearch.search`` — synchronous — from an
    executor thread (and the remote grader's ``KBSearch`` from a gRPC thread) with the
    loop still running. A backend may bridge its judge search either way: a fresh
    ``asyncio.run`` in the calling thread, or ``run_coroutine_threadsafe`` onto the
    loop it built the index on. Both work under this shape; calling the judge's search
    from inside the loop would break the first and deadlock the second.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="conformance-runner-loop"
        )
        self._thread.start()

    def run(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        """Await ``coroutine`` on the loop, as the runner's ``_run_async`` does."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=_CALL_TIMEOUT_S)

    def off_loop(self, call: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Call a synchronous ``call`` from another thread while the loop keeps running."""
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conformance-judge")
        try:
            return pool.submit(call, *args, **kwargs).result(timeout=_CALL_TIMEOUT_S)
        finally:
            pool.shutdown(wait=False)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=_CALL_TIMEOUT_S)
        self._loop.close()


def _search_both_sides(
    loop: RunnerLoop, backend: SearchBackend, corpus_dir: Path, query: str
) -> tuple[SearchIndex, SearchOutcome, list[SearchHit] | None]:
    index = loop.run(backend.build_index(corpus_dir))
    outcome = loop.run(index.search(query, {"query": query}, budget_s=_BUDGET_S))
    knowledge_search = index.knowledge_search()
    judge_hits = (
        None
        if knowledge_search is None
        else loop.off_loop(knowledge_search.search, query, top_k=max(len(outcome.hits), 1))
    )
    return index, outcome, judge_hits


class SearchBackendConformanceSuite:
    """Subclass and override ``backend_factory`` and ``make_searches_fail``."""

    @pytest.fixture
    def backend_factory(self) -> SearchBackendFactory:
        raise NotImplementedError(
            "subclasses of SearchBackendConformanceSuite must override the "
            "`backend_factory` fixture to return a SearchBackendFactory — the same "
            "callable the `tolokaforge.search_backends` entry point resolves to"
        )

    @pytest.fixture
    def make_searches_fail(self) -> Callable[[SearchIndex], None]:
        raise NotImplementedError(
            "subclasses of SearchBackendConformanceSuite must override the "
            "`make_searches_fail` fixture to return a callable that, given a built index, "
            "makes its searches fail the way they do in production (the service goes "
            "away, a request errors) — the suite checks that such a failure is raised "
            "rather than rendered as results"
        )

    @pytest.fixture(name="trial_context")
    def trial_context_fixture(self) -> SearchBackendContext:
        """The runner's context; override to hand the backend its stack-service handle."""
        return trial_context()

    @pytest.fixture
    def corpus_dir(self, tmp_path: Path) -> Path:
        return write_corpus(tmp_path / "corpus")

    @pytest.fixture
    def query(self) -> str:
        return CONFORMANCE_QUERY

    @pytest.fixture
    def runner_loop(self) -> Iterator[RunnerLoop]:
        loop = RunnerLoop()
        yield loop
        loop.close()

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

    def test_stack_service_is_a_declared_stack_service(
        self, backend_factory: SearchBackendFactory
    ) -> None:
        """The orchestrator and the runner refuse a backend naming any other service."""
        stack_service = backend_factory(declaration_context()).stack_service
        if stack_service is None:
            return
        assert stack_service in DECLARED_STACK_SERVICES, (
            f"stack_service {stack_service!r} is not a declared stack service "
            f"{sorted(DECLARED_STACK_SERVICES)}; the runner reaches only the services "
            "tolokaforge.core.search.stack_services declares, so a task selecting this "
            "backend is refused at load"
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
                "backend with no trial and no stack-service handle to read the agent's "
                f"tool schema, and the stack rule to read stack_service: {exc}"
            ) from exc
        assert isinstance(backend, SearchBackend)

    def test_build_index_refuses_a_trial_less_context(
        self, backend_factory: SearchBackendFactory, corpus_dir: Path, runner_loop: RunnerLoop
    ) -> None:
        """Trial work belongs to the runner's context; a trial-less one builds nothing."""
        backend = backend_factory(declaration_context())
        try:
            runner_loop.run(backend.build_index(corpus_dir))
        except Exception:  # noqa: BLE001 — any refusal is the conforming answer
            return
        raise AssertionError(
            "build_index built an index from a trial-less context; that context is what "
            "the adapter and the stack rule build a backend from, so an index built from it "
            "belongs to no trial"
        )

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
        runner_loop: RunnerLoop,
    ) -> None:
        """The agent reads ``rendered``; the judge's search, replay and the remote grader
        are the readers ``hits`` is shaped for."""
        index, outcome, _ = _search_both_sides(
            runner_loop, backend_factory(trial_context), corpus_dir, query
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
        runner_loop: RunnerLoop,
    ) -> None:
        """``knowledge_search()`` is the same index, or ``None`` for no judge search."""
        index, outcome, judge_hits = _search_both_sides(
            runner_loop, backend_factory(trial_context), corpus_dir, query
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

    def test_a_failed_search_raises_rather_than_rendering_results(
        self,
        backend_factory: SearchBackendFactory,
        make_searches_fail: Callable[[SearchIndex], None],
        trial_context: SearchBackendContext,
        corpus_dir: Path,
        query: str,
        runner_loop: RunnerLoop,
    ) -> None:
        """An agent reading "no documents" from a dead index is graded for the outage."""
        index = runner_loop.run(backend_factory(trial_context).build_index(corpus_dir))
        make_searches_fail(index)
        try:
            outcome = runner_loop.run(index.search(query, {"query": query}, budget_s=_BUDGET_S))
        except Exception:  # noqa: BLE001 — raising is the conforming answer
            outcome = None
        assert outcome is None, (
            f"a failed search answered {outcome!r}; the runner returns `rendered` to the "
            "agent as a successful call, so a failure must raise"
        )
        knowledge_search = index.knowledge_search()
        if knowledge_search is None:
            return
        try:
            judge_hits = runner_loop.off_loop(knowledge_search.search, query)
        except Exception:  # noqa: BLE001 — raising is the conforming answer
            return
        raise AssertionError(
            f"the judge's search over a failed index answered {judge_hits!r}; "
            "KnowledgeSearch must raise, never degrade a failure into empty results"
        )

    def test_a_corpus_with_no_documents_is_refused(
        self,
        backend_factory: SearchBackendFactory,
        trial_context: SearchBackendContext,
        tmp_path: Path,
        runner_loop: RunnerLoop,
    ) -> None:
        """A declared corpus that indexes empty is a bundling bug, not an agent failure."""
        empty = tmp_path / "empty_corpus"
        empty.mkdir(exist_ok=True)
        backend = backend_factory(trial_context)
        try:
            runner_loop.run(backend.build_index(empty))
        except SearchIndexBuildError:
            return
        raise AssertionError(
            "build_index accepted a corpus with no documents; RegisterTrial must refuse the "
            "trial with a SearchIndexBuildError rather than let the agent search nothing"
        )
