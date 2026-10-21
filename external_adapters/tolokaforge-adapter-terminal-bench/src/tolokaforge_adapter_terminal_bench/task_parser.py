"""Parse terminal-bench task directories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import tomllib
import yaml


class TaskShape(Enum):
    """Where a terminal-bench task runs its verifier.

    The axis that actually changed across terminal-bench versions is verifier
    placement, not the version number, so the adapter dispatches on this rather
    than on a release label. A new shape slots in as one more member plus a rule
    in :func:`detect_shape`; call sites branch on the member they care about.

    * ``SINGLE_CONTAINER`` — Terminal-Bench 2.1 and earlier: the suite runs inside
      the agent's own container, reading its reward from a shared log path.
    * ``SEPARATE_VERIFIER`` — Terminal-Bench 3.0 and 4.0: the suite runs in a
      dedicated verifier container built from ``tests/Dockerfile``, into which the
      task's declared ``artifacts`` are re-materialised from the agent container
      first. Separate-verifier mode was introduced at 3.0; 4.0 is the same format
      with task resources recalibrated, so both load through this one shape.
    """

    SINGLE_CONTAINER = "single_container"
    SEPARATE_VERIFIER = "separate_verifier"


def detect_shape(
    *, environment_mode: str | None, artifacts: list[Any], has_tests_dockerfile: bool
) -> TaskShape:
    """The task's shape, from the three independent separate-verifier markers.

    Any one of a shipped ``tests/Dockerfile``, ``[verifier].environment_mode``
    set to ``"separate"``, or top-level ``artifacts`` means the suite runs in its
    own container; a task carrying none of them is the single-container shape. The
    markers are OR-ed, not required together, because a real converted task may
    carry only a subset. The mode is matched on the value ``"separate"``, not on
    presence — ``environment_mode = "shared"`` is the single-container mode and
    must not be read as a separate-verifier marker.
    """
    if has_tests_dockerfile or environment_mode == "separate" or bool(artifacts):
        return TaskShape.SEPARATE_VERIFIER
    return TaskShape.SINGLE_CONTAINER


@dataclass(frozen=True)
class TerminalBenchArtifact:
    """A path the separate verifier reads out of the agent's container.

    Declared top-level in a v4 ``task.toml``. ``source`` is the absolute path of
    a state root in the agent container; ``exclude`` lists glob patterns the task
    does not want carried across (caches, regenerable build output). For the
    single-container 2.1 shape this list is empty.
    """

    source: str
    exclude: list[str] = field(default_factory=list)
    src_service: str | None = None
    """Trial service the dump lives in, or ``None`` for the agent's own container.
    Set for a sidecar snapshot (``service=`` on the artifact), which a matching
    collect hook wrote inside that sidecar."""


@dataclass(frozen=True)
class TerminalBenchCollectHook:
    """A command run in a trial container to snapshot state before artifacts are read.

    From a ``[[verifier.collect]]`` entry. ``command`` writes some state to a file
    (a DB dump, file permissions, …) that a matching artifact then carries into
    the verifier. ``service`` is the trial service to run in — a DB sidecar; ``None``
    (the common no-``service`` form) means the agent's own container.
    """

    command: str
    service: str | None = None
    timeout_sec: float = 300.0


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
    environment_mode: str | None = None
    """How the task runs its verifier: ``"separate"`` for a task whose tests run
    in their own container, ``None`` for the single-container shape whose tests
    run in the agent's container. Read from ``[verifier].environment_mode``.
    """
    artifacts: list[TerminalBenchArtifact] = field(default_factory=list)
    """State roots the separate verifier re-materialises from the agent container.

    Empty for the single-container shape, which has no container boundary to
    carry files across.
    """
    collect: list[TerminalBenchCollectHook] = field(default_factory=list)
    """Pre-grade snapshot commands from ``[[verifier.collect]]`` — empty unless the
    task has a stateful sidecar (a database) to dump before artifacts are read."""
    has_tests_dockerfile: bool = False
    """Whether the task ships ``tests/Dockerfile``, i.e. a self-contained verifier
    image. One of the three markers that identify the separate-verifier shape."""
    has_tests_compose: bool = False
    """Whether the task ships ``tests/docker-compose.yaml`` — the tier-3 marker: the
    verifier runs as a stack (its own DB sidecar(s) + a main), not a lone container."""
    shape: TaskShape = TaskShape.SINGLE_CONTAINER
    """Where this task runs its verifier; see :class:`TaskShape`. Derived from the
    three markers at discovery, so call sites dispatch on one field rather than
    re-deriving the rule. Defaults to the single-container shape."""
    harness_skills_dir: str | None = None
    """Task-relative directory of skills the pack ships for a coding-harness
    CLI, as ``task.yaml`` declared it, or ``None`` when it declares none.

    A harness whose :attr:`~tolokaforge_coding_harnesses.HarnessSpec.skills_dir_target`
    names a destination gets this directory delivered there by the run's
    :class:`~tolokaforge_coding_harnesses.protocols.SkillDelivery`
    — one more image layer under the shipped default. The bundle rides with the
    task rather than with the operator's home directory, so what the agent could
    read is versioned alongside the tests it is scored against."""

    @property
    def is_v4(self) -> bool:
        """Whether this task runs its tests in a separate verifier container.

        A convenience over :attr:`shape` for the common separate-verifier check;
        the suite runs in its own container rather than the agent's.
        """
        return self.shape is TaskShape.SEPARATE_VERIFIER


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


def _parse_artifacts(task_id: str, toml_data: Mapping[str, Any]) -> list[TerminalBenchArtifact]:
    """State roots the separate verifier re-materialises, from top-level ``artifacts``.

    Each entry is either a bare string path or a table with a ``source`` key and
    an optional ``exclude`` list. ``artifacts`` is deliberately read from the top
    level, not from ``[verifier]``: a nested table is dropped on the wire, so a
    task that put it there would silently lose its re-materialisation set.
    """
    declared = toml_data.get("artifacts")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise ValueError(
            f"terminal-bench task {task_id!r}: top-level artifacts must be a list; "
            f"got {declared!r}."
        )
    parsed: list[TerminalBenchArtifact] = []
    for entry in declared:
        if isinstance(entry, str):
            parsed.append(TerminalBenchArtifact(source=entry))
            continue
        if isinstance(entry, Mapping):
            source = entry.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"terminal-bench task {task_id!r}: each artifact table needs a "
                    f"non-blank 'source'; got {entry!r}."
                )
            exclude_raw = entry.get("exclude", [])
            if not isinstance(exclude_raw, list) or not all(
                isinstance(x, str) for x in exclude_raw
            ):
                raise ValueError(
                    f"terminal-bench task {task_id!r}: artifact 'exclude' for {source!r} "
                    f"must be a list of strings; got {exclude_raw!r}."
                )
            src_service = entry.get("service")
            if src_service is not None and (
                not isinstance(src_service, str) or not src_service.strip()
            ):
                raise ValueError(
                    f"terminal-bench task {task_id!r}: artifact 'service' for {source!r} "
                    f"must be a non-blank string; got {src_service!r}."
                )
            parsed.append(
                TerminalBenchArtifact(
                    source=source, exclude=list(exclude_raw), src_service=src_service
                )
            )
            continue
        raise ValueError(
            f"terminal-bench task {task_id!r}: each artifact must be a path string or a "
            f"table with a 'source' key; got {entry!r}."
        )
    return parsed


def _parse_collect(task_id: str, verifier: Mapping[str, Any]) -> list[TerminalBenchCollectHook]:
    """Snapshot hooks from ``[[verifier.collect]]`` — empty when none are declared.

    Each entry declares the ``command`` that writes state to a file; ``service``
    is optional (``None`` → the agent's container) and ``timeout_sec`` is optional
    (default 300s).
    """
    declared = verifier.get("collect")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise ValueError(
            f"terminal-bench task {task_id!r}: [[verifier.collect]] must be a list of "
            f"tables; got {declared!r}."
        )
    hooks: list[TerminalBenchCollectHook] = []
    for entry in declared:
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"terminal-bench task {task_id!r}: each verifier.collect entry must be a "
                f"table; got {entry!r}."
            )
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"terminal-bench task {task_id!r}: a verifier.collect entry needs a "
                f"non-blank 'command'; got {entry!r}."
            )
        service = entry.get("service")
        if service is not None and (not isinstance(service, str) or not service.strip()):
            raise ValueError(
                f"terminal-bench task {task_id!r}: verifier.collect 'service' must be a "
                f"non-blank string when set; got {service!r}."
            )
        timeout = entry.get("timeout_sec", 300.0)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError(
                f"terminal-bench task {task_id!r}: verifier.collect timeout_sec must be a "
                f"positive number; got {timeout!r}."
            )
        hooks.append(
            TerminalBenchCollectHook(command=command, service=service, timeout_sec=float(timeout))
        )
    return hooks


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


def parse_task_dir(task_dir: Path) -> TerminalBenchTask:
    """Parse one terminal-bench task directory into a :class:`TerminalBenchTask`.

    *task_dir* holds the task's ``task.toml`` (or the legacy ``task.yaml``) and
    its name is the task id. Shared by :func:`discover_tasks` and the adapter's
    standalone-trial seeding path, so a task mounted alone on a node is parsed
    exactly as it would be during a full discovery scan.
    """
    task_id = task_dir.name
    compose_file = _find_compose(task_dir)

    yaml_data = _load_task_yaml(task_dir)
    instruction = _parse_instruction(task_dir, yaml_data)
    toml_data = _parse_task_toml(task_dir)

    metadata = toml_data.get("metadata", {})
    agent = toml_data.get("agent", {})
    verifier = toml_data.get("verifier", {})
    environment = toml_data.get("environment", {})

    environment_mode = verifier.get("environment_mode")
    if environment_mode is not None and not isinstance(environment_mode, str):
        raise ValueError(
            f"terminal-bench task {task_id!r}: verifier.environment_mode must be a "
            f"string; got {environment_mode!r}."
        )

    artifacts = _parse_artifacts(task_id, toml_data)
    has_tests_dockerfile = (task_dir / "tests" / "Dockerfile").is_file()
    shape = detect_shape(
        environment_mode=environment_mode,
        artifacts=artifacts,
        has_tests_dockerfile=has_tests_dockerfile,
    )
    if shape is TaskShape.SEPARATE_VERIFIER and not has_tests_dockerfile:
        # The verifier container is built from ``tests/Dockerfile``; a task that
        # declares the separate-verifier shape (via artifacts or environment_mode)
        # without shipping it cannot be run. Fail here with the reason rather than
        # letting the per-trial compose build die on a missing Dockerfile.
        raise ValueError(
            f"terminal-bench task {task_id!r}: declares the separate-verifier shape "
            "(top-level artifacts or verifier.environment_mode='separate') but ships no "
            "tests/Dockerfile to build its verifier container from."
        )

    return TerminalBenchTask(
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
        environment_mode=environment_mode,
        artifacts=artifacts,
        collect=_parse_collect(task_id, verifier),
        has_tests_dockerfile=has_tests_dockerfile,
        has_tests_compose=(task_dir / "tests" / "docker-compose.yaml").is_file(),
        shape=shape,
        harness_skills_dir=_parse_harness_skills_dir(task_id, task_dir, yaml_data),
    )


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
        tasks[task_dir.name] = parse_task_dir(task_dir)

    return tasks
