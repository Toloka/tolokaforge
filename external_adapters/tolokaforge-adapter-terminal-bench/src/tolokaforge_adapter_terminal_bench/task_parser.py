"""Parse terminal-bench task directories."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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


def _parse_task_yaml(task_dir: Path) -> str:
    """Extract instruction text from task.yaml."""
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return ""
    with open(task_yaml) as f:
        data = yaml.safe_load(f)
    if data is None:
        return ""
    # task.yaml may have 'instruction' as a string
    instruction = data.get("instruction", "")
    if not instruction:
        # Fallback: try instruction.md
        instruction_md = task_dir / "instruction.md"
        if instruction_md.exists():
            instruction = instruction_md.read_text()
    return instruction


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

        instruction = _parse_task_yaml(task_dir)
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
        )

    return tasks
