"""``EnvironmentState.hydrate`` reads both declared shapes of ``initial_state.json_db``.

An inline mapping is the seed itself, a string is a JSON file under the task
directory, and ``None`` is no seed. A string naming a file that does not exist
fails the hydrate instead of starting the trial on an empty store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.models import InitialStateConfig

pytestmark = pytest.mark.unit


def _hydrated(task_dir: Path, json_db: str | dict | None) -> EnvironmentState:
    env = EnvironmentState(task_dir, InitialStateConfig(json_db=json_db))
    env.hydrate()
    return env


def test_an_inline_mapping_is_the_seed(tmp_path: Path) -> None:
    seed = {"tickets": []}

    env = _hydrated(tmp_path, seed)

    assert env.get_db() == {"tickets": []}
    env.db_state["tickets"].append({"id": "T-1"})
    assert seed == {"tickets": []}


def test_a_file_reference_is_read_from_the_task_dir(tmp_path: Path) -> None:
    (tmp_path / "db.json").write_text(json.dumps({"tickets": [{"id": "T-100"}]}))

    assert _hydrated(tmp_path, "db.json").get_db() == {"tickets": [{"id": "T-100"}]}


def test_no_json_db_is_an_empty_store(tmp_path: Path) -> None:
    assert _hydrated(tmp_path, None).get_db() == {}


def test_a_missing_json_db_file_fails_the_hydrate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="initial_state.json_db file not found"):
        _hydrated(tmp_path, "missing.json")
