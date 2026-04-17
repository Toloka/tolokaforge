"""Predefined service stacks for TolokaForge.

Provides factory functions that return pre-configured ServiceStack instances
for common deployment scenarios.

Available stacks:
    - core_stack: DB service + Runner (minimum for integration tests)
    - full_stack: Core + RAG service + Mock Web
    - test_stack: Core with auto-allocated ports (for CI/testing)

Individual service definitions:
    - typesense_service: TypeSense search server

Example:
    >>> from tolokaforge.docker.stacks import core_stack
    >>> stack = core_stack()
    >>> stack.start_all()
"""

from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.stacks.full import full_stack
from tolokaforge.docker.stacks.test import test_stack
from tolokaforge.docker.stacks.typesense import typesense_service

__all__ = [
    "core_stack",
    "full_stack",
    "test_stack",
    "typesense_service",
]
