"""The corpus roots, and the project layer a task file loads under.

A guard that walks the shipped packs has to load each ``task.yaml`` the way a run
loads it — under the ``task_defaults`` of the enclosing project, or under nothing
at all when the pack ships without one. Both corpus roots bound the search, so a
project-less pack under ``tests/data`` stops at that root instead of walking on to
the repository root and the filesystem above it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tolokaforge.core.project_loader import load_project_config

__all__ = ["EXAMPLES_ROOT", "REPO_ROOT", "TEST_DATA_ROOT", "enclosing_project", "project_layer"]

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = REPO_ROOT / "examples"
TEST_DATA_ROOT = REPO_ROOT / "tests" / "data"


def enclosing_project(task_yaml: Path) -> Path | None:
    """The ``project.yaml`` whose layer this task loads under, or ``None``."""
    for directory in task_yaml.parents:
        candidate = directory / "project.yaml"
        if candidate.exists():
            return candidate
        if directory in (EXAMPLES_ROOT, TEST_DATA_ROOT):
            return None
    return None


def project_layer(task_yaml: Path) -> dict[str, Any] | None:
    """The ``task_defaults`` layer beneath this task, or ``None`` for no project at all.

    ``None`` is the honest answer for a project-less pack rather than an empty
    mapping: no layer means the task's own block *is* the effective one, which is a
    different statement from a layer that could not be read.
    """
    project_yaml = enclosing_project(task_yaml)
    if project_yaml is None:
        return None
    return load_project_config(project_yaml).task_defaults.model_dump(exclude_defaults=True) or None
