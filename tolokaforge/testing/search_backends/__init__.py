"""The search-backend conformance kit an external backend runs against itself.

``tolokaforge.search_backends`` lets a package supply the retrieval a task names in
``initial_state.rag.backend`` (``search.plane`` on the wire). The
:class:`~tolokaforge.core.search.backend.SearchBackend` Protocol states what such a
backend owes the engine, but each obligation is read by someone else — the adapter,
the stack rule, the runner, the judge — so a break produces a wrong artifact, not an
exception.

Three things ship here:

- :class:`SearchBackendConformanceSuite` — the pytest suite an implementer points at
  their own factory. Two fixtures to override — the factory, and how its search
  fails; the assertions are behavioural. :class:`RunnerLoop` is the threading shape
  it runs a backend in, the runner's.
- :class:`InMemorySearchBackend` — the reference implementation and the worked
  example to copy, with a call log the engine's own end-to-end tests read. Its
  :class:`SearchBackendDefects` knobs switch obligations off one at a time, which is
  how the suite's own teeth are proven.
- :func:`in_memory_search_backend_factory` — the reference factory.

Adoption::

    import pytest
    from tolokaforge.testing.search_backends import SearchBackendConformanceSuite

    class TestMyBackendConformance(SearchBackendConformanceSuite):
        @pytest.fixture
        def backend_factory(self):
            return my_search_backend_factory

        @pytest.fixture
        def make_searches_fail(self):
            return lambda index: my_service.go_down()
"""

from .conformance import (
    CONFORMANCE_QUERY,
    RunnerLoop,
    SearchBackendConformanceSuite,
    declaration_context,
    trial_context,
    write_corpus,
)
from .in_memory import (
    IN_MEMORY_SEARCH_BACKEND_NAME,
    InMemoryKnowledgeSearch,
    InMemorySearchBackend,
    InMemorySearchCallLog,
    InMemorySearchIndex,
    SearchBackendDefects,
    in_memory_search_backend_factory,
)

__all__ = [
    "CONFORMANCE_QUERY",
    "IN_MEMORY_SEARCH_BACKEND_NAME",
    "InMemoryKnowledgeSearch",
    "InMemorySearchBackend",
    "InMemorySearchCallLog",
    "InMemorySearchIndex",
    "RunnerLoop",
    "SearchBackendConformanceSuite",
    "SearchBackendDefects",
    "declaration_context",
    "in_memory_search_backend_factory",
    "trial_context",
    "write_corpus",
]
