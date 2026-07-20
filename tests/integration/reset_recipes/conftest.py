"""Pytest configuration for the reset-recipe integration suite.

``testcontainers`` passes no ``--project-name`` and runs Docker Compose with
``cwd`` set to the context dir, so Compose derives the project name from that
dir's basename. Every reset-recipe test builds its stack under
``tmp_path / "compose"``, so they all resolve to project name ``compose`` —
harmless sequentially, but under ``pytest -n auto`` two such stacks on
different workers share one project namespace and collide on container /
network names. Pinning ``COMPOSE_PROJECT_NAME`` per worker keeps every
worker's stacks disjoint in one place, without editing each call site.

Scoped to this suite: other integration tests derive per-test project names
from slug-encoded ``make_project_temp_dir`` basenames and must not have that
overridden by a per-worker env var.
"""

from __future__ import annotations

import os

import pytest


def apply_compose_project_isolation() -> str:
    """Set and return this worker's ``COMPOSE_PROJECT_NAME``.

    Reads xdist's ``PYTEST_XDIST_WORKER`` (e.g. ``gw0``); absent it (no
    xdist) falls back to one stable name.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    name = f"tolokaforge-it-{worker}"
    os.environ["COMPOSE_PROJECT_NAME"] = name
    return name


@pytest.fixture(scope="session", autouse=True)
def isolate_compose_project_per_worker() -> None:
    """Pin one ``COMPOSE_PROJECT_NAME`` per xdist worker so concurrent
    reset-recipe stacks on different workers never share a Compose project
    namespace.

    Per-worker uniqueness suffices because a worker runs its tests
    sequentially and each reset-recipe test boots a single stack.
    """
    apply_compose_project_isolation()
