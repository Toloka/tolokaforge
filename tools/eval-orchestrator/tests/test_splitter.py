"""Tests for eval_orchestrator.splitter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from eval_orchestrator.splitter import (
    distribute_tasks,
    resolve_tasks,
    split,
    write_matrix_json,
    write_shard_configs,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def task_tree(tmp_path: Path) -> Path:
    """Create a minimal task tree with 6 tasks."""
    for i in range(6):
        task_dir = tmp_path / "tasks" / f"task_{i:03d}"
        task_dir.mkdir(parents=True)
        task_yaml = task_dir / "task.yaml"
        task_yaml.write_text(
            yaml.dump(
                {
                    "task_id": f"TASK-{i:03d}",
                    "prompt": f"Do thing {i}",
                }
            )
        )
    return tmp_path


@pytest.fixture()
def config_data(task_tree: Path) -> dict:
    """Return a minimal run config dict pointing to the task_tree."""
    return {
        "models": {
            "agent": {"provider": "openrouter", "name": "openai/gpt-5.4", "temperature": 0.4},
            "user": {"provider": "openrouter", "name": "openai/gpt-5.4", "temperature": 0.2},
        },
        "evaluation": {
            "tasks_glob": "tasks/**/task.yaml",
            "output_dir": "output/test",
        },
        "orchestrator": {
            "workers": 8,
            "repeats": 1,
            "max_turns": 20,
            "runtime": "docker",
        },
    }


@pytest.fixture()
def config_file(task_tree: Path, config_data: dict) -> Path:
    """Write config to a YAML file inside task_tree."""
    config_path = task_tree / "run_config.yaml"
    config_path.write_text(yaml.dump(config_data))
    return config_path


# ---------- resolve_tasks ----------


def test_resolve_tasks_finds_all(task_tree: Path, config_data: dict) -> None:
    tasks = resolve_tasks(config_data, task_tree)
    assert len(tasks) == 6
    for t in tasks:
        assert t.name == "task.yaml"


def test_resolve_tasks_empty_on_bad_glob(task_tree: Path) -> None:
    data = {"evaluation": {"tasks_glob": "nonexistent/**/task.yaml"}}
    tasks = resolve_tasks(data, task_tree)
    assert tasks == []


def test_resolve_tasks_with_task_packs(task_tree: Path) -> None:
    data = {
        "evaluation": {
            "tasks_glob": "**/task.yaml",
            "task_packs": [str(task_tree / "tasks")],
        }
    }
    tasks = resolve_tasks(data, task_tree)
    assert len(tasks) == 6


# ---------- distribute_tasks ----------


def test_round_robin_distribution() -> None:
    tasks = [Path(f"/t/task_{i}/task.yaml") for i in range(7)]
    shards = distribute_tasks(tasks, 3)
    assert len(shards) == 3
    # 7 tasks over 3 shards: 3, 2, 2
    assert len(shards[0]) == 3
    assert len(shards[1]) == 2
    assert len(shards[2]) == 2
    # All tasks accounted for
    assert sum(len(s) for s in shards) == 7


def test_distribute_fewer_tasks_than_shards() -> None:
    tasks = [Path(f"/t/task_{i}/task.yaml") for i in range(2)]
    shards = distribute_tasks(tasks, 5)
    non_empty = [s for s in shards if s]
    assert len(non_empty) == 2


def test_distribute_single_shard() -> None:
    tasks = [Path(f"/t/task_{i}/task.yaml") for i in range(4)]
    shards = distribute_tasks(tasks, 1)
    assert len(shards) == 1
    assert len(shards[0]) == 4


# ---------- write_shard_configs ----------


def test_write_shard_configs_creates_files(task_tree: Path, config_data: dict) -> None:
    tasks = resolve_tasks(config_data, task_tree)
    task_shards = distribute_tasks(tasks, 2)
    output_dir = task_tree / "shards"

    configs = write_shard_configs(config_data, task_shards, output_dir, workers_per_shard=3)

    assert len(configs) == 2
    for cfg_path in configs:
        assert cfg_path.exists()
        with open(cfg_path) as f:
            shard = yaml.safe_load(f)
        assert shard["orchestrator"]["workers"] == 3
        assert "task_packs" not in shard.get("evaluation", {})
        assert "task.yaml" in shard["evaluation"]["tasks_glob"]


def test_write_shard_configs_creates_symlinks(task_tree: Path, config_data: dict) -> None:
    tasks = resolve_tasks(config_data, task_tree)
    task_shards = distribute_tasks(tasks, 2)
    output_dir = task_tree / "shards"

    write_shard_configs(config_data, task_shards, output_dir, workers_per_shard=3)

    for i in range(2):
        shard_tasks_dir = output_dir / f"shard_{i}" / "tasks"
        assert shard_tasks_dir.is_dir()
        links = list(shard_tasks_dir.iterdir())
        assert len(links) > 0
        for link in links:
            assert link.is_symlink() or link.is_dir()


# ---------- write_matrix_json ----------


def test_write_matrix_json(tmp_path: Path) -> None:
    configs = [tmp_path / "shard_0.yaml", tmp_path / "shard_1.yaml"]
    matrix = write_matrix_json(configs, tmp_path)

    assert "include" in matrix
    assert len(matrix["include"]) == 2
    assert matrix["include"][0]["shard_index"] == 0
    assert matrix["include"][1]["shard_index"] == 1

    # File written
    matrix_file = tmp_path / "matrix.json"
    assert matrix_file.exists()
    loaded = json.loads(matrix_file.read_text())
    assert loaded == matrix


# ---------- split (integration) ----------


def test_split_end_to_end(config_file: Path, task_tree: Path) -> None:
    output_dir = task_tree / "ci_shards"
    matrix = split(
        config_path=config_file,
        num_shards=3,
        workers_per_shard=2,
        output_dir=output_dir,
        base_dir=task_tree,
    )

    assert len(matrix["include"]) == 3
    for entry in matrix["include"]:
        cfg_path = Path(entry["config"])
        assert cfg_path.exists()
        with open(cfg_path) as f:
            shard = yaml.safe_load(f)
        assert shard["orchestrator"]["workers"] == 2
        assert shard["evaluation"]["output_dir"].startswith("output/shard-")


def test_split_caps_shards_at_task_count(config_file: Path, task_tree: Path) -> None:
    """When requesting more shards than tasks, effective shards are capped."""
    output_dir = task_tree / "ci_shards_capped"
    matrix = split(
        config_path=config_file,
        num_shards=20,  # more than the 6 tasks
        workers_per_shard=1,
        output_dir=output_dir,
        base_dir=task_tree,
    )
    assert len(matrix["include"]) == 6


def test_split_raises_on_no_tasks(tmp_path: Path) -> None:
    config_data = {
        "models": {
            "agent": {"provider": "openrouter", "name": "test", "temperature": 0.0},
        },
        "evaluation": {
            "tasks_glob": "nonexistent/**/task.yaml",
            "output_dir": "output/test",
        },
        "orchestrator": {"workers": 1, "repeats": 1, "max_turns": 10, "runtime": "docker"},
    }
    config_path = tmp_path / "empty.yaml"
    config_path.write_text(yaml.dump(config_data))

    with pytest.raises(ValueError, match="No tasks found"):
        split(config_path, num_shards=2, workers_per_shard=1, output_dir=tmp_path / "out")
