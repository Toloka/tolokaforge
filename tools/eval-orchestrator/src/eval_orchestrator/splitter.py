"""Split a tolokaforge run config into N shard configs for parallel CI execution.

The splitter resolves the tasks_glob from the input config, discovers all task
directories, distributes them across N shards using round-robin, and writes
per-shard config YAMLs plus a GitHub Actions matrix JSON.
"""

from __future__ import annotations

import glob as glob_module
import json
from pathlib import Path

import yaml


def resolve_tasks(config_data: dict, base_dir: Path) -> list[Path]:
    """Resolve tasks_glob from a run config into a sorted list of task.yaml paths.

    Applies the same glob logic used by tolokaforge adapters: resolve the
    ``evaluation.tasks_glob`` pattern relative to *base_dir* (or iterate over
    ``evaluation.task_packs`` when present).

    Returns:
        Sorted list of absolute ``Path`` objects pointing to task.yaml files.
    """
    evaluation = config_data.get("evaluation", {})
    tasks_glob = evaluation.get("tasks_glob", "**/task.yaml")
    task_packs: list[str] = evaluation.get("task_packs", [])

    task_files: list[Path] = []

    if task_packs:
        for pack_root in task_packs:
            pack_path = Path(pack_root)
            pattern = str(pack_path / tasks_glob)
            for match in glob_module.glob(pattern, recursive=True):
                task_files.append(Path(match).resolve())
    else:
        pattern = str(base_dir / tasks_glob)
        for match in glob_module.glob(pattern, recursive=True):
            task_files.append(Path(match).resolve())

    # Deduplicate and sort for deterministic ordering
    return sorted(set(task_files))


def distribute_tasks(task_files: list[Path], num_shards: int) -> list[list[Path]]:
    """Distribute task files across shards using round-robin.

    Returns:
        List of N lists, where each inner list contains the task paths for that
        shard.  Some trailing shards may be empty when ``len(task_files) < num_shards``.
    """
    shards: list[list[Path]] = [[] for _ in range(num_shards)]
    for idx, task_path in enumerate(task_files):
        shards[idx % num_shards].append(task_path)
    return shards


def _build_shard_config(
    config_data: dict,
    shard_tasks_glob: str,
    shard_output_dir: str,
    workers: int,
) -> dict:
    """Build a shard config dict from the original config, overriding key fields."""
    shard = json.loads(json.dumps(config_data))  # deep copy

    shard.setdefault("evaluation", {})["tasks_glob"] = shard_tasks_glob
    shard["evaluation"]["output_dir"] = shard_output_dir
    # Clear task_packs since shard configs use absolute symlinked paths
    shard["evaluation"].pop("task_packs", None)

    shard.setdefault("orchestrator", {})["workers"] = workers

    return shard


def write_shard_configs(
    config_data: dict,
    task_shards: list[list[Path]],
    output_dir: Path,
    workers_per_shard: int,
) -> list[Path]:
    """Write per-shard config YAMLs and symlink task directories.

    For each shard:
      1. Create ``output_dir/shard_{i}/tasks/`` with symlinks to actual task
         directories (the parent of each task.yaml).
      2. Write ``output_dir/shard_{i}.yaml`` with ``tasks_glob`` pointing to
         that symlinked directory.

    Returns:
        List of paths to the written shard config files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_configs: list[Path] = []

    for shard_idx, shard_tasks in enumerate(task_shards):
        if not shard_tasks:
            continue

        # Create symlink directory for this shard's tasks
        shard_tasks_dir = output_dir / f"shard_{shard_idx}" / "tasks"
        shard_tasks_dir.mkdir(parents=True, exist_ok=True)

        for task_yaml in shard_tasks:
            task_dir = task_yaml.parent
            link_name = shard_tasks_dir / task_dir.name
            # Handle potential name collisions by using a more unique name
            if link_name.exists() or link_name.is_symlink():
                # Use longer path component to disambiguate
                unique_name = f"{task_dir.parent.name}__{task_dir.name}"
                link_name = shard_tasks_dir / unique_name

            if not link_name.exists():
                link_name.symlink_to(task_dir, target_is_directory=True)

        # tasks_glob relative to repo root (will be resolved by the adapter)
        shard_glob = str(shard_tasks_dir) + "/**/task.yaml"
        shard_output = f"output/shard-{shard_idx}"

        shard_config = _build_shard_config(
            config_data,
            shard_tasks_glob=shard_glob,
            shard_output_dir=shard_output,
            workers=workers_per_shard,
        )

        config_path = output_dir / f"shard_{shard_idx}.yaml"
        with open(config_path, "w") as f:
            yaml.dump(shard_config, f, default_flow_style=False, sort_keys=False)

        shard_configs.append(config_path)

    return shard_configs


def write_matrix_json(shard_configs: list[Path], output_dir: Path) -> dict:
    """Write matrix.json for GitHub Actions and return the matrix dict.

    The matrix contains an ``include`` array with one entry per shard:
    ``{"shard_index": i, "config": "<path>"}``.
    """
    include = []
    for idx, config_path in enumerate(shard_configs):
        include.append(
            {
                "shard_index": idx,
                "config": str(config_path),
            }
        )

    matrix = {"include": include}

    matrix_path = output_dir / "matrix.json"
    with open(matrix_path, "w") as f:
        json.dump(matrix, f, indent=2)

    return matrix


def split(
    config_path: Path,
    num_shards: int,
    workers_per_shard: int,
    output_dir: Path,
    *,
    base_dir: Path | None = None,
) -> dict:
    """Top-level split entrypoint.

    1. Parse config
    2. Resolve tasks
    3. Distribute across shards
    4. Write shard configs + symlinks
    5. Write matrix.json

    Args:
        base_dir: Directory to resolve ``tasks_glob`` against.
            Defaults to ``cwd()`` (project root), matching how tolokaforge
            adapters resolve globs.

    Returns:
        The matrix dict (also written to ``output_dir/matrix.json``).
    """
    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    if base_dir is None:
        base_dir = Path.cwd()
    tasks_glob = config_data.get("evaluation", {}).get("tasks_glob", "")

    task_files = resolve_tasks(config_data, base_dir)

    if not task_files:
        raise ValueError(
            f"No tasks found for glob '{tasks_glob}' resolved from base_dir={base_dir}"
        )

    # Cap actual shards at task count
    effective_shards = min(num_shards, len(task_files))
    task_shards = distribute_tasks(task_files, effective_shards)

    shard_configs = write_shard_configs(
        config_data,
        task_shards,
        output_dir,
        workers_per_shard,
    )

    matrix = write_matrix_json(shard_configs, output_dir)

    return matrix
