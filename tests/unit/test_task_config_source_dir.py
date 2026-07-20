"""``TaskConfig._source_dir`` locator: non-serialising, loader-stamped.

Locks that the in-process source-dir locator (a) never leaks onto the
serialisation surface and (b) is stamped by the task loader / native adapter
to the effective task directory (domain root for shared-domain tasks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters._task_loader import _detect_task_root, load_task_yaml
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import TaskConfig

pytestmark = pytest.mark.unit


def _flat_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
        },
    )
    return task_dir / "task.yaml"


def _shared_domain_task(tmp_path: Path) -> Path:
    root = tmp_path / "dataset" / "dom"
    shared = root / "_shared"
    write_yaml_file(shared / "domain.yaml", {"category": "tool_use"})
    case_dir = root / "testcases" / "case_a"
    case_dir.mkdir(parents=True)
    write_yaml_file(
        case_dir / "task.yaml",
        {
            "task_id": "dom_case_a",
            "description": "dom case a",
            "domain": "../../_shared/domain.yaml",
        },
    )
    return case_dir / "task.yaml"


class TestNotOnSerialisationSurface:
    """(a) The locator is a private in-process value, never serialised."""

    def test_locator_absent_from_model_dump_and_schema(self) -> None:
        task = TaskConfig(task_id="t", description="d")
        task._source_dir = Path("/some/pack/dir")

        assert "_source_dir" not in task.model_dump()
        assert "source_dir" not in task.model_dump()
        assert "_source_dir" not in TaskConfig.model_json_schema()["properties"]
        assert "source_dir" not in TaskConfig.model_json_schema()["properties"]

    def test_round_trip_through_model_dump_is_unaffected(self) -> None:
        task = TaskConfig(task_id="t", description="d")
        task._source_dir = Path("/some/pack/dir")

        clone = TaskConfig(**task.model_dump())

        assert clone.model_dump() == task.model_dump()
        # The locator does not survive a serialisation round-trip.
        assert clone.source_dir is None


class TestLoaderStampsSourceDir:
    """(b) A loaded task carries its effective task dir."""

    def test_flat_layout_loader_stamps_task_parent(self, tmp_path: Path) -> None:
        task_path = _flat_task(tmp_path)
        task, task_dir = load_task_yaml(task_path)

        assert task.source_dir == task_dir == _detect_task_root(task_path)
        assert task.source_dir == task_path.parent

    def test_shared_domain_loader_stamps_domain_root(self, tmp_path: Path) -> None:
        task_path = _shared_domain_task(tmp_path)
        task, task_dir = load_task_yaml(task_path)

        domain_root = task_path.parent.parent.parent
        assert task.source_dir == task_dir == _detect_task_root(task_path) == domain_root

    def test_native_adapter_get_task_carries_source_dir(self, tmp_path: Path) -> None:
        _shared_domain_task(tmp_path)
        adapter = NativeAdapter(
            {
                "base_dir": str(tmp_path / "dataset"),
                "tasks_glob": "dom/testcases/**/task.yaml",
            }
        )
        task = adapter.get_task("dom_case_a")

        assert task.source_dir == adapter.get_task_dir("dom_case_a")
