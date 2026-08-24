"""The seeded-table view a task's ``initial_state.json_db`` describes.

One reading of what a native task seeds, shared by the task-description build
the runner is handed and by the authoring gates that hold a grading declaration
against it. The shapes an author may write are not uniform — a collection is a
list of records, records keyed by id, or one record written inline — and a
second implementation of that normalisation is a second set of tables to
disagree about, which is exactly what the equality lock below forbids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml, seeded_tables_from_task
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _write_pack(root: Path, *, initial_state: dict | None, seeded: dict | None = None) -> Path:
    """Emit a minimal native pack at ``root/tasks/widgets/`` and return its task dir."""
    task_dir = root / "tasks" / "widgets"
    task_dir.mkdir(parents=True)
    task: dict[str, Any] = {
        "task_id": "widgets",
        "name": "Widgets test",
        "category": "test",
        "description": "seeded-table fixture",
        "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
        "user_simulator": {
            "mode": "scripted",
            "scripted_flow": [{"role": "user", "content": "hi"}],
        },
        "policies": {"disallowed_actions": [], "guidance": []},
    }
    if initial_state is not None:
        task["initial_state"] = initial_state
    (task_dir / "task.yaml").write_text(yaml.safe_dump(task))
    if seeded is not None:
        (task_dir / "initial_state.json").write_text(json.dumps(seeded))
    return task_dir


def _seeded_tables(task_dir: Path) -> dict[str, list[dict[str, Any]]]:
    task, effective_dir = load_task_yaml(task_dir / "task.yaml")
    return seeded_tables_from_task(task, effective_dir)


@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        pytest.param(
            [{"widget_id": "W1"}, {"widget_id": "W2"}],
            [{"widget_id": "W1"}, {"widget_id": "W2"}],
            id="list_of_records",
        ),
        pytest.param(
            {"W1": {"widget_id": "W1"}, "W2": {"widget_id": "W2"}},
            [{"widget_id": "W1"}, {"widget_id": "W2"}],
            id="dict_of_records_keyed_by_id",
        ),
        pytest.param(
            {"currency": "USD", "locale": "en"},
            [{"currency": "USD", "locale": "en"}],
            id="single_record_of_primitives",
        ),
        pytest.param({}, [{}], id="empty_mapping_is_one_empty_record"),
    ],
)
def test_each_authored_collection_shape_normalises_to_its_records(
    tmp_path: Path, collection: Any, expected: list[dict[str, Any]]
) -> None:
    task_dir = _write_pack(
        tmp_path,
        initial_state={"json_db": "initial_state.json"},
        seeded={"widgets": collection},
    )
    assert _seeded_tables(task_dir) == {"widgets": expected}


def test_inline_json_db_is_read_as_the_file_would_be(tmp_path: Path) -> None:
    seeded = {"widgets": [{"widget_id": "W1"}], "settings": {"currency": "USD"}}
    inline = _write_pack(tmp_path / "inline", initial_state={"json_db": seeded})
    on_disk = _write_pack(
        tmp_path / "on_disk",
        initial_state={"json_db": "initial_state.json"},
        seeded=seeded,
    )
    assert _seeded_tables(inline) == _seeded_tables(on_disk)
    assert _seeded_tables(inline) == {
        "widgets": [{"widget_id": "W1"}],
        "settings": [{"currency": "USD"}],
    }


@pytest.mark.parametrize(
    "initial_state",
    [
        pytest.param(None, id="no_initial_state"),
        pytest.param({"initialization_actions": []}, id="initial_state_without_json_db"),
    ],
)
def test_a_task_seeding_nothing_seeds_no_tables(tmp_path: Path, initial_state: dict | None) -> None:
    task_dir = _write_pack(tmp_path, initial_state=initial_state)
    assert _seeded_tables(task_dir) == {}


def test_a_json_db_file_not_on_disk_raises_naming_the_path(tmp_path: Path) -> None:
    task_dir = _write_pack(tmp_path, initial_state={"json_db": "initial_state.json"})
    with pytest.raises(RuntimeError) as ei:
        _seeded_tables(task_dir)
    assert str(task_dir / "initial_state.json") in str(ei.value)


def test_the_task_description_carries_the_same_tables_this_parser_reads(tmp_path: Path) -> None:
    # Every shape at once: the adapter and the gates must agree on all of them,
    # not merely on the list case an author most often writes.
    task_dir = _write_pack(
        tmp_path,
        initial_state={"json_db": "initial_state.json"},
        seeded={
            "positions": [{"account_id": "A1", "symbol": "AAPL"}],
            "accounts": {"A1": {"account_id": "A1"}, "A2": {"account_id": "A2"}},
            "settings": {"currency": "USD", "locale": "en"},
        },
    )
    adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
    described = adapter.to_task_description("widgets").initial_state.tables

    assert described == _seeded_tables(task_dir)
    assert set(described) == {"positions", "accounts", "settings"}
