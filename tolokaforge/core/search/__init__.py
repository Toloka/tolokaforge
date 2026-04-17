"""
Search interfaces for TolokaForge.

This module provides abstract interfaces for search backends like TypeSense.
"""

from .domain_state import DomainState, DomainStateManager, DomainStatus
from .typesense import TypeSenseClient, TypeSenseStub
from .typesense_provider import (
    TypeSenseProvider,
    TypeSenseProviderConfig,
    create_typesense_provider,
)
from .typesense_server import (
    DOCKER_AVAILABLE,
    TypeSenseServerManager,
    create_typesense_server,
    find_free_port,
    generate_api_key,
)

__all__ = [
    # Domain state management
    "DomainState",
    "DomainStateManager",
    "DomainStatus",
    # TypeSense client interfaces
    "TypeSenseClient",
    "TypeSenseStub",
    # TypeSense provider
    "TypeSenseProvider",
    "TypeSenseProviderConfig",
    "create_typesense_provider",
    # TypeSense server management
    "TypeSenseServerManager",
    "create_typesense_server",
    "find_free_port",
    "generate_api_key",
    "DOCKER_AVAILABLE",
]
