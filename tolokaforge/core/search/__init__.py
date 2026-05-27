"""
Search interfaces for TolokaForge.

This module provides abstract interfaces for search backends like TypeSense.
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
