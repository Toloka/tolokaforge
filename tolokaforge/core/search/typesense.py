"""
Search result data classes for the TypeSense subsystem.

These are part of the supported adapter-facing surface: an adapter that
needs to expose TypeSense results in a structured form can import
``SearchResponse`` and ``SearchResult`` from ``tolokaforge.core.search``.

The engine deliberately does *not* ship a ``TypeSenseClient`` abstraction.
Adapters wanting a real client should use the ``typesense`` Python package
directly — it is already a hard dependency of the engine. See
``docs/TYPESENSE_INTEGRATION.md`` for the engine/adapter split.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """A single search result."""

    document_id: str
    score: float
    content: dict[str, Any]
    highlights: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Response from a search query."""

    hits: list[SearchResult]
    total_hits: int
    query: str
    search_time_ms: float
