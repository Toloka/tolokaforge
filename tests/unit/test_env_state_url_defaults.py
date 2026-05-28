"""Verify EnvironmentState does not leak Docker-internal URLs by default.

Backports the contract from opensource commit 14aad388c: defaults must be
empty so executor/orchestrator code can decide when to populate URLs
explicitly. Empty defaults mean tasks that don't use mock_web (etc.)
won't leak hostnames into env.yaml.
"""

from pathlib import Path

import pytest

from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.models import InitialStateConfig

pytestmark = pytest.mark.unit


def _make_env_state(tmp_path: Path) -> EnvironmentState:
    return EnvironmentState(
        task_dir=tmp_path,
        initial_state_config=InitialStateConfig(),
    )


def test_service_urls_default_to_empty(tmp_path: Path) -> None:
    env = _make_env_state(tmp_path)
    assert env.json_db_url == ""
    assert env.rag_service_url == ""
    assert env.mock_web_url == ""


def test_final_state_omits_mock_web_url_when_unset(tmp_path: Path) -> None:
    env = _make_env_state(tmp_path)
    env.hydrate()
    state = env.get_final_state()
    assert (
        "mock_web_url" not in state
    ), "mock_web_url leaked into final state with default empty URL"


def test_final_state_includes_mock_web_url_when_explicitly_set(tmp_path: Path) -> None:
    env = _make_env_state(tmp_path)
    env.hydrate()
    env.mock_web_url = "http://browser-host:8080"
    state = env.get_final_state()
    assert state["mock_web_url"] == "http://browser-host:8080"
