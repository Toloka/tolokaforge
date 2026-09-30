"""The search-backend conformance kit an external backend runs against itself.

``tolokaforge.search_backends`` lets a package supply the retrieval a task names in
``initial_state.rag.backend`` (``search.plane`` on the wire). The
:class:`~tolokaforge.core.search.backend.SearchBackend` Protocol states what such a
backend owes the engine, but each obligation is read by someone else — the adapter,
the stack rule, the runner, the judge — so a break produces a wrong artifact, not an
exception.

Three things ship here:

- :class:`SearchBackendConformanceSuite` — the pytest suite an implementer points at
  their own factory. One fixture to override; the assertions are behavioural.
- :class:`InMemorySearchBackend` — the reference implementation and the worked
  example to copy, with a call log the engine's own end-to-end tests read. Its
  :class:`SearchBackendDefects` knobs switch obligations off one at a time, which is
  how the suite's own teeth are proven.
- :func:`in_memory_search_backend_factory` — the reference factory.

Adoption is five lines::

    import pytest
    from tolokaforge.testing.search_backends import SearchBackendConformanceSuite

    class TestMyBackendConformance(SearchBackendConformanceSuite):
        @pytest.fixture
        def backend_factory(self):
            return my_search_backend_factory
"""

from .conformance import (
    CONFORMANCE_QUERY,
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
    "SearchBackendConformanceSuite",
    "SearchBackendDefects",
    "declaration_context",
    "in_memory_search_backend_factory",
    "trial_context",
    "write_corpus",
]
