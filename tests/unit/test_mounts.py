"""Unit tests for task-pack mount planning."""

from pathlib import Path

import pytest

from tolokaforge.core.mounts import (
    build_task_pack_mount_plan,
    compose_override_from_mount_plan,
    normalize_task_pack_paths,
)

pytestmark = pytest.mark.unit


def test_normalize_task_pack_paths_resolves_relative(tmp_path: Path):
    config_path = tmp_path / "config" / "run.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("evaluation: {}")

    rel_pack = tmp_path / "config" / "packs" / "core"
    rel_pack.mkdir(parents=True)

    paths = normalize_task_pack_paths(["packs/core"], config_path)
    assert paths == [rel_pack.resolve()]


def test_normalize_task_pack_paths_raises_for_missing(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text("evaluation: {}")

    with pytest.raises(FileNotFoundError, match="Task-pack path does not exist"):
        normalize_task_pack_paths(["./missing"], config_path)


def test_build_mount_plan_and_override(tmp_path: Path):
    pack_a = tmp_path / "pack_a"
    pack_b = tmp_path / "pack_b"
    pack_a.mkdir()
    pack_b.mkdir()

    plan = build_task_pack_mount_plan([pack_a.resolve(), pack_b.resolve()])
    assert plan.container_roots == ["/taskpacks/0", "/taskpacks/1"]
    assert plan.task_packs_dirs_env == "/taskpacks/0,/taskpacks/1"
    assert plan.tasks_dirs_env == "/app/tasks,/taskpacks/0,/taskpacks/1"

    override = compose_override_from_mount_plan(plan)
    assert "runner" in override["services"]
    runner = override["services"]["runner"]
    assert runner["environment"]["TASKS_DIRS"] == "/app/tasks,/taskpacks/0,/taskpacks/1"
    assert runner["environment"]["TASK_PACKS_DIRS"] == "/taskpacks/0,/taskpacks/1"
    assert len(runner["volumes"]) == 2
