"""Parse terminal-bench task directories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib
import yaml


@dataclass
class TerminalBenchTask:
    """Parsed metadata for a single terminal-bench task."""

    task_id: str
    task_dir: Path
    compose_file: Path
    instruction: str
    difficulty: str = "medium"
    tags: list[str] = field(default_factory=list)
    agent_timeout_sec: float = 1800.0
    verifier_timeout_sec: float = 120.0
    cpus: float = 2
    memory_mb: int = 4096
    harness_skills_dir: str | None = None
    """Task-relative directory of skills the pack ships for a coding-harness
    CLI, as ``task.yaml`` declared it, or ``None`` when it declares none.

    A harness whose :attr:`~tolokaforge_adapter_terminal_bench.harness.HarnessSpec.skills_dir_target`
    names a destination gets this directory copied into its image layer. The
    bundle rides with the task rather than with the operator's home directory,
    so what the agent could read is versioned alongside the tests it is scored
    against."""


def _load_task_yaml(task_dir: Path) -> dict[str, Any]:
    """Contents of the task's ``task.yaml``, empty when it declares none."""
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return {}
    data = yaml.safe_load(task_yaml.read_text())
    return data if isinstance(data, dict) else {}


def _parse_instruction(task_dir: Path, data: Mapping[str, Any]) -> str:
    """Instruction text from ``task.yaml``, falling back to ``instruction.md``."""
    instruction = data.get("instruction", "")
    if not instruction:
        instruction_md = task_dir / "instruction.md"
        if instruction_md.exists():
            instruction = instruction_md.read_text()
    return instruction


def _parse_harness_skills_dir(task_id: str, task_dir: Path, data: Mapping[str, Any]) -> str | None:
    """The declared skills bundle, refused unless it is inside the task pack.

    Containment is checked after resolution, so neither a ``..`` segment nor a
    symlink pointing out of the pack can reach the operator's own skills — the
    contamination the parity policy exists to keep out of a benchmark image.
    """
    declared = data.get("harness_skills_dir")
    if declared is None:
        return None
    if not isinstance(declared, str) or not declared.strip():
        raise ValueError(
            f"terminal-bench task {task_id!r}: harness_skills_dir must be a non-blank "
            f"task-relative path; got {declared!r}."
        )
    if Path(declared).is_absolute():
        raise ValueError(
            f"terminal-bench task {task_id!r}: harness_skills_dir {declared!r} is an "
            "absolute path; declare a path relative to the task directory so the bundle "
            "travels with the pack."
        )
    resolved = (task_dir / declared).resolve()
    if not resolved.is_relative_to(task_dir.resolve()):
        raise ValueError(
            f"terminal-bench task {task_id!r}: harness_skills_dir {declared!r} resolves to "
            f"{resolved}, outside the task directory {task_dir}; a skills bundle must ship "
            "inside the task pack."
        )
    if not resolved.is_dir():
        raise ValueError(
            f"terminal-bench task {task_id!r}: harness_skills_dir {declared!r} is not a "
            f"directory inside the task pack (looked at {resolved})."
        )
    return declared


def _parse_task_toml(task_dir: Path) -> dict:
    """Parse task.toml for metadata and resource limits."""
    task_toml = task_dir / "task.toml"
    if not task_toml.exists():
        return {}
    with open(task_toml, "rb") as f:
        return tomllib.load(f)


def discover_tasks(base_dir: Path) -> dict[str, TerminalBenchTask]:
    """Find terminal-bench task directories under *base_dir*.

    A valid task directory must contain both ``docker-compose.yaml`` and
    ``task.yaml`` (or ``task.toml``).
    """
    tasks: dict[str, TerminalBenchTask] = {}

    for compose_file in sorted(base_dir.glob("*/docker-compose.yaml")):
        task_dir = compose_file.parent
        task_id = task_dir.name

        # Must have task.yaml or task.toml
        if not (task_dir / "task.yaml").exists() and not (task_dir / "task.toml").exists():
            continue

        yaml_data = _load_task_yaml(task_dir)
        instruction = _parse_instruction(task_dir, yaml_data)
        toml_data = _parse_task_toml(task_dir)

        metadata = toml_data.get("metadata", {})
        agent = toml_data.get("agent", {})
        verifier = toml_data.get("verifier", {})
        environment = toml_data.get("environment", {})

        tasks[task_id] = TerminalBenchTask(
            task_id=task_id,
            task_dir=task_dir,
            compose_file=compose_file,
            instruction=instruction,
            difficulty=metadata.get("difficulty", "medium"),
            tags=metadata.get("tags", []),
            agent_timeout_sec=agent.get("timeout_sec", 1800.0),
            verifier_timeout_sec=verifier.get("timeout_sec", 120.0),
            cpus=environment.get("cpus", 2),
            memory_mb=environment.get("memory_mb", 4096),
            harness_skills_dir=_parse_harness_skills_dir(task_id, task_dir, yaml_data),
        )

    return tasks
