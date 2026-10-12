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
    compose_file: Path | None
    """The task's own compose file, or ``None`` for the single-container shape.

    ``None`` is the common case: the compose doc is synthesised from
    ``environment/Dockerfile`` at materialisation. A path here means the task
    declares more than one container and owns its own topology.
    """
    instruction: str
    difficulty: str = "medium"
    tags: list[str] = field(default_factory=list)
    agent_timeout_sec: float = 1800.0
    verifier_timeout_sec: float | None = None
    """Seconds the task allows its own verifier, or ``None`` when it declares none.

    ``None`` rather than a number because the grader already owns a default and
    a number invented here would silently outrank it. The value reaches
    :class:`~tolokaforge.core.grading.kinds.TestExecutionKindConfig` only when
    the task actually asked for it.
    """
    cpus: float = 2
    memory_mb: int = 4096
    harness_skills_dir: str | None = None
    """Task-relative directory of skills the pack ships for a coding-harness
    CLI, as ``task.yaml`` declared it, or ``None`` when it declares none.

    A harness whose :attr:`~tolokaforge_coding_harnesses.HarnessSpec.skills_dir_target`
    names a destination gets this directory delivered there by the run's
    :class:`~tolokaforge_coding_harnesses.protocols.SkillDelivery`
    — one more image layer under the shipped default. The bundle rides with the
    task rather than with the operator's home directory, so what the agent could
    read is versioned alongside the tests it is scored against."""


def _load_task_yaml(task_dir: Path) -> dict[str, Any]:
    """Contents of the task's ``task.yaml``, empty when it declares none."""
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return {}
    data = yaml.safe_load(task_yaml.read_text())
    return data if isinstance(data, dict) else {}


def _parse_instruction(task_dir: Path, data: Mapping[str, Any]) -> str:
    """Instruction text from ``task.yaml``, falling back to ``instruction.md``."""
    # A bare ``instruction:`` key parses as None, which ``.get(..., "")`` cannot cover.
    instruction = data.get("instruction") or ""
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


def _parse_verifier_timeout(task_id: str, data: Mapping[str, Any]) -> float | None:
    """The task's declared verifier timeout, refused unless it is a positive number.

    Checked here rather than at the grading kind, which validates the same
    bound but only once a trial has been run: the agent's work would already
    be spent before anything noticed the task could not be graded.
    """
    declared = data.get("timeout_sec")
    if declared is None:
        return None
    if isinstance(declared, bool) or not isinstance(declared, int | float):
        raise ValueError(
            f"terminal-bench task {task_id!r}: verifier.timeout_sec must be a number; "
            f"got {declared!r}."
        )
    if declared <= 0:
        raise ValueError(
            f"terminal-bench task {task_id!r}: verifier.timeout_sec must be greater "
            f"than zero; got {declared!r}."
        )
    return float(declared)


def _find_compose(task_dir: Path) -> Path | None:
    """The task's compose file, or ``None`` when it declares one container.

    Two locations exist in the corpus and they do not overlap:

    * ``environment/docker-compose.yaml`` — the canonical location, and the
      only one the upstream harness reads. 197 of 974 delivered tasks use it
      and 180 of those declare two or more services: a database, a queue, a
      worker. Reading the Dockerfile alone for these would build the agent's
      container and silently drop everything it talks to.
    * ``docker-compose.yaml`` at the task root — 86 tasks, carrying the
      ``${T_BENCH_*}`` variables of the pre-harbor format. Upstream ignores
      these entirely; 85 of the 86 declare a single service that only builds
      ``./environment`` and idles, which is what synthesis produces anyway.
      They are honoured because one of them, ``build-streaming-monetization``,
      really does declare extra services there.

    Canonical first, so a task that somehow grew both gets the one upstream
    would have used.
    """
    for candidate in (
        task_dir / "environment" / "docker-compose.yaml",
        task_dir / "docker-compose.yaml",
    ):
        if candidate.is_file():
            return candidate
    return None


def discover_tasks(base_dir: Path) -> dict[str, TerminalBenchTask]:
    """Find terminal-bench task directories under *base_dir*.

    A task declares itself with ``task.toml`` (or the legacy ``task.yaml``).
    Compose is optional and names the multi-container shape; see
    :func:`_find_compose` for where it is looked for and why in that order.
    The single-container majority ships ``environment/Dockerfile`` alone and
    :func:`~tolokaforge_adapter_terminal_bench.compose_synthesis.materialise_task_environment`
    synthesises the compose doc for it.

    Keying discovery on a compose file instead cost us most of the corpus: of
    974 delivered tasks all 974 carry ``task.toml`` and
    ``environment/Dockerfile`` while 86 carry a root compose file, so 8.8% of
    the benchmark was visible and the rest was silently absent — not skipped
    with a reason, never enumerated at all.
    """
    tasks: dict[str, TerminalBenchTask] = {}

    declared = {p.parent for p in base_dir.glob("*/task.toml")}
    declared |= {p.parent for p in base_dir.glob("*/task.yaml")}

    for task_dir in sorted(declared):
        task_id = task_dir.name
        compose_file = _find_compose(task_dir)

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
            verifier_timeout_sec=_parse_verifier_timeout(task_id, verifier),
            cpus=environment.get("cpus", 2),
            memory_mb=environment.get("memory_mb", 4096),
            harness_skills_dir=_parse_harness_skills_dir(task_id, task_dir, yaml_data),
        )

    return tasks
