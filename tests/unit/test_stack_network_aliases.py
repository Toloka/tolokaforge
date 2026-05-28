"""Service stacks declare network aliases so task YAMLs that reference
short hostnames (``mock-web``, ``rag-service``, ``db-service``, etc.) keep
resolving via Docker DNS even though container names are prefixed
(``tolokaforge-mock-web`` …).

Surfaced when the runner moved fully into Docker — a Chromium navigation
to ``http://mock-web:8080/...`` from inside the runner container hit
``ERR_NAME_NOT_RESOLVED`` because Docker only registers the container
name as a DNS entry by default.
"""

from __future__ import annotations

import pytest

from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.stacks.full import full_stack

pytestmark = pytest.mark.unit


def _svc(stack, name):
    svc = stack.services.get(name)
    assert svc is not None, f"service {name!r} missing from stack"
    return svc


def test_core_stack_db_service_aliases_short_names():
    stack = core_stack()
    assert "db-service" in _svc(stack, "db-service").network_aliases
    assert "json-db" in _svc(stack, "db-service").network_aliases


def test_core_stack_runner_aliases_short_name():
    stack = core_stack()
    assert "runner" in _svc(stack, "runner").network_aliases


def test_full_stack_mock_web_aliases_short_name():
    stack = full_stack()
    assert "mock-web" in _svc(stack, "mock-web").network_aliases


def test_full_stack_rag_service_aliases_short_name():
    stack = full_stack()
    assert "rag-service" in _svc(stack, "rag-service").network_aliases


def test_full_stack_inherits_core_aliases():
    """Drift detection: full_stack still includes the db-service / runner
    aliases that core_stack defines (full_stack composes core_stack)."""
    stack = full_stack()
    assert "db-service" in _svc(stack, "db-service").network_aliases
    assert "json-db" in _svc(stack, "db-service").network_aliases
    assert "runner" in _svc(stack, "runner").network_aliases
