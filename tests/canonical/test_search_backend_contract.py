"""Pin the ``SearchBackend`` seam: the Protocol surface, and the kit's teeth.

Three things are locked here.

**The built-in backends conform.** ``rag_service`` resolves through the
``tolokaforge.search_backends`` registry and runs the whole
:class:`~tolokaforge.testing.search_backends.SearchBackendConformanceSuite` against a
rag-service stand-in that answers the runner's client and the judge's search from one
store, so the kit is proven by the implementation it describes. ``bm25`` runs the same
suite in process; its searches are made to fail the way an in-process index can — the
ranking itself raising.

**The reference fixture conforms.** :class:`InMemorySearchBackend` is the worked example
an external implementer copies; a reference that does not pass the suite teaches the
wrong backend.

**The suite has teeth.** Every obligation is read somewhere other than the backend, so a
backend that breaks one produces a wrong artifact rather than raising. Each
:class:`SearchBackendDefects` knob switches off exactly one obligation, and the assertion
written for it must fail on that backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.utils.fake_rag_service import FakeRagService
from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.plugin_registry import load_search_backend
from tolokaforge.core.search.backend import (
    SearchBackend,
    SearchBackendContext,
    SearchIndex,
    SearchOutcome,
)
from tolokaforge.core.search.bm25 import Bm25SearchBackend, Bm25SearchIndex
from tolokaforge.core.search.stack_services import StackServices
from tolokaforge.runner.rag_service_backend import RagServiceBackend
from tolokaforge.testing.search_backends import (
    InMemorySearchBackend,
    InMemorySearchIndex,
    RunnerLoop,
    SearchBackendConformanceSuite,
    SearchBackendDefects,
    in_memory_search_backend_factory,
    trial_context,
    write_corpus,
)

pytestmark = pytest.mark.canonical


class TestProtocolSurface:
    def test_every_shipped_backend_satisfies_the_protocol(self) -> None:
        for factory in (
            load_search_backend("rag_service"),
            load_search_backend("bm25"),
            in_memory_search_backend_factory,
        ):
            assert isinstance(factory(trial_context()), SearchBackend)

    def test_an_object_without_build_index_is_not_a_backend(self) -> None:
        class _NoIndex:
            name = "x"
            stack_service = None

            def tool_parameters(self) -> Any:
                return None

        assert not isinstance(_NoIndex(), SearchBackend)

    def test_an_object_without_knowledge_search_is_not_an_index(self) -> None:
        class _SearchOnly:
            async def search(self, query: str, arguments: Any, *, budget_s: float) -> Any:
                return None

        assert not isinstance(_SearchOnly(), SearchIndex)


class TestRagServiceConformance(SearchBackendConformanceSuite):
    """``rag_service`` — through the registry, against a rag-service stand-in."""

    @pytest.fixture(autouse=True)
    def rag_service(self, monkeypatch: pytest.MonkeyPatch) -> FakeRagService:
        service = FakeRagService()
        service.route_judge_posts(monkeypatch)
        return service

    @pytest.fixture
    def backend_factory(self) -> Any:
        return load_search_backend("rag_service")

    @pytest.fixture(name="trial_context")
    def trial_context_fixture(self, rag_service: FakeRagService) -> SearchBackendContext:
        return trial_context(stack_services=StackServices(rag_service=rag_service.client()))

    @pytest.fixture
    def make_searches_fail(self, rag_service: FakeRagService) -> Any:
        def fail(index: SearchIndex) -> None:
            rag_service.searches_fail = True

        return fail

    def test_the_suite_ran_the_registered_built_in(
        self, backend_factory: Any, trial_context: SearchBackendContext
    ) -> None:
        assert isinstance(backend_factory(trial_context), RagServiceBackend)


class TestBm25Conformance(SearchBackendConformanceSuite):
    """``bm25`` — through the registry, in process, over the suite's Markdown corpus."""

    @pytest.fixture
    def backend_factory(self) -> Any:
        return load_search_backend("bm25")

    @pytest.fixture
    def make_searches_fail(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        def fail(index: SearchIndex) -> None:
            assert isinstance(index, Bm25SearchIndex)

            def raising(query: str, top_k: int) -> Any:
                raise RuntimeError("the ranking failed")

            # Both the agent's search and the judge's read rank through this one method.
            monkeypatch.setattr(index, "rank", raising)

        return fail

    def test_the_suite_ran_the_registered_built_in(
        self, backend_factory: Any, trial_context: SearchBackendContext
    ) -> None:
        assert isinstance(backend_factory(trial_context), Bm25SearchBackend)


def _fail_in_memory_searches(index: SearchIndex) -> None:
    assert isinstance(index, InMemorySearchIndex)
    index.fail_searches_with(ConnectionError("the index went away"))


class TestInMemoryConformance(SearchBackendConformanceSuite):
    """The reference fixture an external implementer copies."""

    @pytest.fixture
    def backend_factory(self) -> Any:
        return in_memory_search_backend_factory

    @pytest.fixture
    def make_searches_fail(self) -> Any:
        return _fail_in_memory_searches


class _BridgingIndex:
    """An index whose own search is a coroutine, and whose judge search bridges to it.

    The judge's ``KnowledgeSearch.search`` is synchronous and the runner calls it off
    the event loop the index was built on; a backend over an async client reaches its
    coroutine from there either with a fresh ``asyncio.run`` or with
    ``run_coroutine_threadsafe`` onto the loop it captured at build time.
    """

    def __init__(self, inner: InMemorySearchIndex, loop: asyncio.AbstractEventLoop, bridge: str):
        self.inner = inner
        self.loop = loop
        self.bridge = bridge

    async def ranked(self, query: str, top_k: int) -> list[SearchHit]:
        await asyncio.sleep(0)
        return list(self.inner.rank(query, top_k))

    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome:
        return await self.inner.search(query, arguments, budget_s=budget_s)

    def knowledge_search(self) -> _BridgingKnowledgeSearch:
        return _BridgingKnowledgeSearch(self)


class _BridgingKnowledgeSearch:
    def __init__(self, index: _BridgingIndex) -> None:
        self.index = index

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        coroutine = self.index.ranked(query, top_k)
        if self.index.bridge == "asyncio_run":
            return asyncio.run(coroutine)
        return asyncio.run_coroutine_threadsafe(coroutine, self.index.loop).result(timeout=10)


def _bridging_factory(bridge: str) -> Any:
    class _BridgingBackend(InMemorySearchBackend):
        async def build_index(self, corpus_dir: Path | None) -> Any:
            inner = await super().build_index(corpus_dir)
            return _BridgingIndex(inner, asyncio.get_running_loop(), bridge)

    return _BridgingBackend


@pytest.mark.parametrize("bridge", ["asyncio_run", "run_coroutine_threadsafe"])
class TestABackendBridgingItsJudgeSearchToACoroutineConforms(SearchBackendConformanceSuite):
    """The suite calls the judge's search where the runner does: off the running loop.

    Called from inside the loop, the ``asyncio_run`` bridge would raise and the
    ``run_coroutine_threadsafe`` bridge would deadlock — both correct backends.
    """

    @pytest.fixture
    def backend_factory(self, bridge: str) -> Any:
        return _bridging_factory(bridge)

    @pytest.fixture
    def make_searches_fail(self) -> Any:
        def fail(index: SearchIndex) -> None:
            assert isinstance(index, _BridgingIndex)
            _fail_in_memory_searches(index.inner)

        return fail


def _defective(**defects: Any) -> Any:
    def factory(context: SearchBackendContext) -> InMemorySearchBackend:
        return InMemorySearchBackend(context, defects=SearchBackendDefects(**defects))

    return factory


_VECTORS = [
    pytest.param(
        {"needs_a_trial_to_build": True},
        "test_the_factory_builds_from_a_trial_less_context",
        id="factory-needs-a-trial",
    ),
    pytest.param(
        {"parameters_without_query": True},
        "test_tool_parameters_is_a_parameters_object_with_a_query",
        id="parameters-declare-no-query",
    ),
    pytest.param(
        {"hits_as_a_list": True},
        "test_search_returns_hits_and_the_rendered_text",
        id="hits-are-not-a-tuple",
    ),
    pytest.param(
        {"rendered_as_a_mapping": True},
        "test_search_returns_hits_and_the_rendered_text",
        id="rendered-is-not-text",
    ),
    pytest.param(
        {"judge_reads_another_index": True},
        "test_the_judge_reads_the_index_the_agent_searched",
        id="judge-reads-another-index",
    ),
    pytest.param(
        {"indexes_an_empty_corpus": True},
        "test_a_corpus_with_no_documents_is_refused",
        id="empty-corpus-accepted",
    ),
    pytest.param(
        {"builds_without_a_trial": True},
        "test_build_index_refuses_a_trial_less_context",
        id="index-built-for-no-trial",
    ),
    pytest.param(
        {"renders_failed_searches_as_empty": True},
        "test_a_failed_search_raises_rather_than_rendering_results",
        id="failed-search-rendered-as-empty",
    ),
    pytest.param(
        {"declares_an_undeclared_stack_service": True},
        "test_stack_service_is_a_declared_stack_service",
        id="undeclared-stack-service",
    ),
]


def _call(suite: SearchBackendConformanceSuite, test_name: str, factory: Any, tmp: Path) -> None:
    """Drive one suite test directly with the inputs its fixtures would supply."""
    test = getattr(suite, test_name)
    wanted = test.__code__.co_varnames[1 : test.__code__.co_argcount]
    loop = RunnerLoop()
    supplied = {
        "backend_factory": factory,
        "make_searches_fail": _fail_in_memory_searches,
        "trial_context": trial_context(),
        "corpus_dir": write_corpus(tmp / "corpus"),
        "query": "refund window",
        "tmp_path": tmp,
        "runner_loop": loop,
    }
    try:
        test(**{name: supplied[name] for name in wanted})
    finally:
        loop.close()


class TestTheSuiteDetectsEachVector:
    """One defect, one failing assertion — the kit's own regression guard."""

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_named_assertion_fails_on_the_defective_backend(
        self, defects: dict[str, Any], test_name: str, tmp_path: Path
    ) -> None:
        with pytest.raises(AssertionError):
            _call(SearchBackendConformanceSuite(), test_name, _defective(**defects), tmp_path)

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_same_assertion_passes_on_the_conforming_backend(
        self, defects: dict[str, Any], test_name: str, tmp_path: Path
    ) -> None:
        """The control: the assertion passes on the reference with the defect off."""
        _call(
            SearchBackendConformanceSuite(), test_name, in_memory_search_backend_factory, tmp_path
        )


def test_the_kit_is_importable_from_the_distributed_package() -> None:
    import tolokaforge.testing.search_backends as kit

    for name in (
        "SearchBackendConformanceSuite",
        "InMemorySearchBackend",
        "in_memory_search_backend_factory",
    ):
        assert name in kit.__all__
        assert getattr(kit, name) is not None
