"""Integration-tier pytest configuration.

``testcontainers`` passes no ``--project-name`` and runs Docker Compose with
``cwd`` set to the context dir, so Compose derives the project name from that
dir's basename. Integration tests that build their stack under
``tmp_path / "compose"`` therefore all resolve to project name ``compose`` —
harmless sequentially, but under ``pytest -n auto`` two such stacks on
different workers share one project namespace and collide on container /
network names. Pinning ``COMPOSE_PROJECT_NAME`` per worker keeps every
worker's stacks disjoint in one place, without editing each call site.
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
    """Pin one ``COMPOSE_PROJECT_NAME`` per xdist worker so concurrent stacks
    on different workers never share a Compose project namespace.

    Per-worker uniqueness suffices because a worker runs its tests
    sequentially. Assumes no single test boots two mutually-isolated Compose
    stacks concurrently — those would share this per-worker name and collide,
    and must set their own per-stack ``COMPOSE_PROJECT_NAME``.
    """
    apply_compose_project_isolation()
