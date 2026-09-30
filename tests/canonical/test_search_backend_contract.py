"""Pin the ``SearchBackend`` seam: the Protocol surface, and the kit's teeth.

Three things are locked here.

**The built-in backend conforms.** ``rag_service`` resolves through the
``tolokaforge.search_backends`` registry and runs the whole
:class:`~tolokaforge.testing.search_backends.SearchBackendConformanceSuite` against a
rag-service stand-in that answers the runner's client and the judge's search from one
store, so the kit is proven by the implementation it describes.

**The reference fixture conforms.** :class:`InMemorySearchBackend` is the worked example
an external implementer copies; a reference that does not pass the suite teaches the
wrong backend.

**The suite has teeth.** Every obligation is read somewhere other than the backend, so a
backend that breaks one produces a wrong artifact rather than raising. Each
:class:`SearchBackendDefects` knob switches off exactly one obligation, and the assertion
written for it must fail on that backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.utils.fake_rag_service import FakeRagService
from tolokaforge.core.plugin_registry import load_search_backend
from tolokaforge.core.search.backend import (
    SearchBackend,
    SearchBackendContext,
    SearchIndex,
)
from tolokaforge.runner.rag_service_backend import RagServiceBackend
from tolokaforge.testing.search_backends import (
    InMemorySearchBackend,
    SearchBackendConformanceSuite,
    SearchBackendDefects,
    in_memory_search_backend_factory,
    trial_context,
    write_corpus,
)

pytestmark = pytest.mark.canonical


class TestProtocolSurface:
    def test_both_shipped_backends_satisfy_the_protocol(self) -> None:
        for factory in (load_search_backend("rag_service"), in_memory_search_backend_factory):
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
        return trial_context(stack_service_clients={"rag_service": rag_service.client()})

    def test_the_suite_ran_the_registered_built_in(
        self, backend_factory: Any, trial_context: SearchBackendContext
    ) -> None:
        assert isinstance(backend_factory(trial_context), RagServiceBackend)


class TestInMemoryConformance(SearchBackendConformanceSuite):
    """The reference fixture an external implementer copies."""

    @pytest.fixture
    def backend_factory(self) -> Any:
        return in_memory_search_backend_factory


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
]


def _call(suite: SearchBackendConformanceSuite, test_name: str, factory: Any, tmp: Path) -> None:
    """Drive one suite test directly with the inputs its fixtures would supply."""
    test = getattr(suite, test_name)
    wanted = test.__code__.co_varnames[1 : test.__code__.co_argcount]
    supplied = {
        "backend_factory": factory,
        "trial_context": trial_context(),
        "corpus_dir": write_corpus(tmp / "corpus"),
        "query": "refund window",
        "tmp_path": tmp,
    }
    test(**{name: supplied[name] for name in wanted})


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
