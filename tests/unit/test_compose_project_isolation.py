"""Locks the per-worker ``COMPOSE_PROJECT_NAME`` isolation that keeps
parallel (``pytest -n auto``) reset-recipe Docker stacks from colliding.
"""

from __future__ import annotations

import os

import pytest

from tests.integration.reset_recipes.conftest import apply_compose_project_isolation

pytestmark = pytest.mark.unit


def test_distinct_workers_get_distinct_names(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    gw0 = apply_compose_project_isolation()
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    gw1 = apply_compose_project_isolation()
    assert gw0 != gw1


def test_sets_the_env_var_docker_compose_reads(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    returned = apply_compose_project_isolation()
    assert os.environ["COMPOSE_PROJECT_NAME"] == returned == "tolokaforge-it-gw2"


def test_no_xdist_maps_to_one_stable_name(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert apply_compose_project_isolation() == "tolokaforge-it-main"
