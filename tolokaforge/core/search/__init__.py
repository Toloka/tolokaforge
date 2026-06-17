"""
Adapter-facing search primitives.

This package exposes the supported building blocks adapters use to integrate
TypeSense-backed search with the engine. The engine owns container lifecycle
(``TypeSenseServerManager`` and ``docker/stacks/typesense.py``) and the
thread-safe domain-init coordination plumbing (``domain_state``); adapters
own their own provider — including any benchmark- or domain-specific
indexing strategy and any benchmark-specific dependencies.

The names exported from this package are the **supported public contract**:
removing or renaming one is a breaking change. New names land here only
when an adapter actually needs them.

See ``docs/TYPESENSE_INTEGRATION.md`` for the engine/adapter split and a
worked example using the ``typesense`` Python package directly.
"""

from .domain_state import DomainState, DomainStateManager, DomainStatus
from .typesense import SearchResponse, SearchResult

__all__ = [
    # Domain init coordination (thread-safe state machine)
    "DomainState",
    "DomainStateManager",
    "DomainStatus",
    # Result data classes
    "SearchResponse",
    "SearchResult",
]
