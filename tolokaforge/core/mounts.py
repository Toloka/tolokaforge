"""Task-pack mount planning utilities for Docker runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskPackMountPlan:
    """Container mount and environment plan derived from task-pack roots."""

    host_roots: list[Path]
    container_roots: list[str]
    volumes: list[str]
    tasks_dirs_env: str
    task_packs_dirs_env: str


def normalize_task_pack_paths(task_packs: list[str], config_path: Path) -> list[Path]:
    """Resolve task-pack paths against config directory and validate existence."""
    base_dir = config_path.parent.resolve()
    normalized: list[Path] = []

    for raw in task_packs:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()
        else:
            candidate = candidate.resolve()

        if not candidate.exists():
            raise FileNotFoundError(
                "Task-pack path does not exist: "
                f"{candidate}. Update evaluation.task_packs or create the path before docker run."
            )

        normalized.append(candidate)

    return normalized


def build_task_pack_mount_plan(task_pack_roots: list[Path]) -> TaskPackMountPlan:
    """Build mount/env plan consumed by docker-compose orchestration."""
    container_roots = [f"/taskpacks/{idx}" for idx in range(len(task_pack_roots))]
    volumes = [
        f"{path}:{container}:ro" for path, container in zip(task_pack_roots, container_roots)
    ]

    tasks_dirs = ["/app/tasks", *container_roots]

    return TaskPackMountPlan(
        host_roots=task_pack_roots,
        container_roots=container_roots,
        volumes=volumes,
        tasks_dirs_env=",".join(tasks_dirs),
        task_packs_dirs_env=",".join(container_roots),
    )


def compose_override_from_mount_plan(plan: TaskPackMountPlan) -> dict:
    """Render docker-compose override structure from a mount plan."""
    return {
        "services": {
            "runner": {
                "volumes": plan.volumes.copy(),
                "environment": {
                    "TASKS_DIRS": plan.tasks_dirs_env,
                    "TASK_PACKS_DIRS": plan.task_packs_dirs_env,
                },
            },
        },
    }
