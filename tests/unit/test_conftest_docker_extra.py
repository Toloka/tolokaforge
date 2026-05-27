"""Verify conftest exposes the _DOCKER_EXTRA_FIXTURES contract."""

import pytest

from tests import conftest

EXPECTED = {
    "env_network",
    "env_files_volume",
    "rag_data_volume",
    "json_db_container",
    "rag_service_container",
    "runner_container",
}


pytestmark = pytest.mark.unit


def test_docker_extra_fixtures_attr_exists() -> None:
    assert hasattr(conftest, "_DOCKER_EXTRA_FIXTURES")
    assert isinstance(conftest._DOCKER_EXTRA_FIXTURES, list)


def test_docker_extra_fixtures_populated_when_extra_installed() -> None:
    # Normal dev/CI path: [docker] extra is installed, all six fixtures register.
    assert set(conftest._DOCKER_EXTRA_FIXTURES) == EXPECTED


def test_docker_extra_fixtures_in_all() -> None:
    for name in EXPECTED:
        assert name in conftest.__all__
