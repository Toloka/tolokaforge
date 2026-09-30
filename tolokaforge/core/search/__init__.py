"""
Search interfaces for TolokaForge.

:mod:`tolokaforge.core.search.backend` holds the ``SearchBackend`` seam a task's
knowledge-base search resolves through (ADR-0052); it is imported by its own path.
This package re-exports the TypeSense client interfaces and domain state.
"""

from .domain_state import DomainState, DomainStateManager, DomainStatus
from .typesense import TypeSenseClient, TypeSenseStub

__all__ = [
    # Domain state management
    "DomainState",
    "DomainStateManager",
    "DomainStatus",
    # TypeSense client interfaces
    "TypeSenseClient",
    "TypeSenseStub",
]
