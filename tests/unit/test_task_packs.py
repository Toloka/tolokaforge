"""Unit tests for task-pack aware discovery and config parsing."""

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import RunConfig
from tolokaforge.env.mock_web_service.app import _parse_tasks_roots

pytestmark = pytest.mark.unit


def _write_minimal_task_yaml(path: Path, task_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump({"task_id": task_id}, f)


def test_run_config_parses_task_packs():
    config_data = {
        "evaluation": {
            "task_packs": ["/tmp/pack_a", "/tmp/pack_b"],
            "tasks_glob": "**/task.yaml",
            "output_dir": "output/test",
        },
        "models": {
            "agent": {"provider": "openai", "name": "gpt-4o-mini"},
        },
        "orchestrator": {"workers": 1, "repeats": 1},
    }

    run_config = RunConfig(**config_data)
    assert run_config.evaluation.task_packs == ["/tmp/pack_a", "/tmp/pack_b"]
    assert run_config.evaluation.tasks_glob == "**/task.yaml"


def test_native_adapter_discovers_tasks_from_multiple_task_packs(tmp_path: Path):
    pack_a = tmp_path / "pack_a"
    pack_b = tmp_path / "pack_b"

    _write_minimal_task_yaml(pack_a / "tasks" / "browser" / "task_a" / "task.yaml", "task_a")
    _write_minimal_task_yaml(pack_b / "tasks" / "mobile" / "task_b" / "task.yaml", "task_b")
    _write_minimal_task_yaml(
        pack_a / "tasks" / "browser" / "shared_a" / "task.yaml",
        "shared_id",
    )
    _write_minimal_task_yaml(
        pack_b / "tasks" / "browser" / "shared_b" / "task.yaml",
        "shared_id",
    )

    adapter = NativeAdapter(
        {
            "tasks_glob": "tasks/**/task.yaml",
            "task_packs": [str(pack_a), str(pack_b)],
        }
    )

    task_ids = adapter.get_task_ids()
    assert "task_a" in task_ids
    assert "task_b" in task_ids
    assert "shared_id" in task_ids

    # Duplicate task_id should keep first pack match deterministically.
    shared_dir = adapter.get_task_dir("shared_id")
    assert str(shared_dir).startswith(str(pack_a))


def test_parse_tasks_roots_normalizes_pack_root_and_tasks_root(tmp_path: Path):
    pack_root = tmp_path / "pack"
    tasks_root = pack_root / "tasks"
    direct_tasks_root = tmp_path / "direct_tasks"
    (tasks_root / "mobile").mkdir(parents=True)
    direct_tasks_root.mkdir(parents=True)

    parsed = _parse_tasks_roots(
        tasks_dirs_env=f"{pack_root},{direct_tasks_root}",
        tasks_dir_env=None,
    )

    assert tasks_root.resolve() in parsed
    assert direct_tasks_root.resolve() in parsed


def test_parse_tasks_roots_malformed_tasks_dirs_falls_back_to_tasks_dir(tmp_path: Path):
    fallback_root = tmp_path / "fallback" / "tasks"
    fallback_root.mkdir(parents=True)

    parsed = _parse_tasks_roots(tasks_dirs_env=",, ,", tasks_dir_env=str(fallback_root))
    assert parsed == [fallback_root.resolve()]


def test_absolute_tasks_glob_with_task_packs_fails(tmp_path: Path):
    pack_a = tmp_path / "pack_a"
    pack_b = tmp_path / "pack_b"

    _write_minimal_task_yaml(pack_a / "tasks" / "browser" / "task_a" / "task.yaml", "task_a")
    _write_minimal_task_yaml(pack_b / "tasks" / "browser" / "task_b" / "task.yaml", "task_b")

    with pytest.raises(ValueError, match="tasks_glob must be relative"):
        adapter = NativeAdapter(
            {
                "tasks_glob": str((pack_b / "tasks" / "**" / "task.yaml").resolve()),
                "task_packs": [str(pack_a)],
            }
        )
        adapter.get_task_ids()


def test_invalid_task_yaml_emits_warning_and_skips(tmp_path: Path):
    from tolokaforge.adapters import native as native_mod

    pack = tmp_path / "pack"
    bad_task = pack / "tasks" / "browser" / "bad_task" / "task.yaml"
    bad_task.parent.mkdir(parents=True, exist_ok=True)
    bad_task.write_text("task_id: [unterminated\n", encoding="utf-8")

    adapter = NativeAdapter(
        {
            "tasks_glob": "tasks/**/task.yaml",
            "task_packs": [str(pack)],
        }
    )

    # Clear log history before test
    native_mod.logger.logs.clear()
    task_ids = adapter.get_task_ids()
    assert task_ids == []
    # Check the custom logger's internal log store
    warning_messages = [e["message"] for e in native_mod.logger.logs if e["level"] == "WARNING"]
    assert any("Invalid task file; skipping" in m for m in warning_messages)


def test_missing_task_id_emits_warning_and_skips(tmp_path: Path):
    from tolokaforge.adapters import native as native_mod

    pack = tmp_path / "pack"
    missing_id = pack / "tasks" / "browser" / "no_id" / "task.yaml"
    missing_id.parent.mkdir(parents=True, exist_ok=True)
    missing_id.write_text("name: no-id-task\n", encoding="utf-8")

    adapter = NativeAdapter(
        {
            "tasks_glob": "tasks/**/task.yaml",
            "task_packs": [str(pack)],
        }
    )

    # Clear log history before test
    native_mod.logger.logs.clear()
    task_ids = adapter.get_task_ids()
    assert task_ids == []
    # Check the custom logger's internal log store
    warning_messages = [e["message"] for e in native_mod.logger.logs if e["level"] == "WARNING"]
    assert any("Task file missing task_id; skipping" in m for m in warning_messages)
