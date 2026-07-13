"""Shared Docker Compose task contract for benchmark adapters."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from tolokaforge.core.models import TaskConfig
from tolokaforge.runner.models import InvocationStyle, ToolSchema, ToolSource


class ComposeToolSourceConfig(BaseModel):
    """Validated ``tools.agent.<tool>.source`` compose declaration."""

    invocation_style: Literal["docker_compose_exec"]
    compose_file: str
    service: str
    tests_dir: str
    env_vars: dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


def load_grading_data(task: TaskConfig, task_dir: Path) -> dict[str, Any]:
    """Load the task grading mapping and surface missing/invalid files."""

    grading_path = _resolve_task_path(task_dir, task.grading, label="grading")
    if not grading_path.is_file():
        raise ValueError(f"Grading file not found: {grading_path}")
    grading_data = yaml.safe_load(grading_path.read_text(encoding="utf-8")) or {}
    if not isinstance(grading_data, dict):
        raise ValueError(f"Grading file must contain a mapping: {grading_path}")
    return grading_data


def _resolve_task_path(task_dir: Path, value: str, *, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} must be a safe path relative to the task directory: {value!r}")
    root = task_dir.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes the task directory: {value!r}")
    return resolved


def validate_compose_source(
    tool_name: str,
    source_data: dict[str, Any],
    task_dir: Path,
    *,
    grading_method: str | None,
) -> ComposeToolSourceConfig:
    """Validate files, service, tests, and grading before Runner startup."""

    try:
        config = ComposeToolSourceConfig.model_validate(source_data)
    except Exception as exc:
        raise ValueError(f"Invalid compose source for tools.agent.{tool_name}: {exc}") from exc

    if grading_method != "test_execution":
        raise ValueError(
            f"tools.agent.{tool_name}.source uses docker_compose_exec but grading.yaml "
            "must declare grading_method: test_execution"
        )

    compose_path = _resolve_task_path(task_dir, config.compose_file, label="compose_file")
    if not compose_path.is_file():
        raise ValueError(f"Compose file not found for tools.agent.{tool_name}: {compose_path}")
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"Could not parse compose file {compose_path}: {exc}") from exc
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict) or config.service not in services:
        available = sorted(services) if isinstance(services, dict) else []
        raise ValueError(
            f"Compose service {config.service!r} for tools.agent.{tool_name} was not found "
            f"in {compose_path}; available services: {available}"
        )

    tests_path = _resolve_task_path(task_dir, config.tests_dir, label="tests_dir")
    if not tests_path.is_dir():
        raise ValueError(f"Tests directory not found for tools.agent.{tool_name}: {tests_path}")
    test_script = tests_path / "test.sh"
    if not test_script.is_file():
        raise ValueError(
            f"test_execution grading requires {config.tests_dir}/test.sh for "
            f"tools.agent.{tool_name}"
        )
    return config


def validate_native_compose_contract(
    task: TaskConfig,
    task_dir: Path,
    grading_data: dict[str, Any] | None,
) -> list[tuple[str, ComposeToolSourceConfig]]:
    """Validate every native compose source and its grading compatibility."""

    grading_method = grading_data.get("grading_method") if grading_data else None
    declarations: list[tuple[str, ComposeToolSourceConfig]] = []
    enabled = task.tools.agent.get("enabled", []) if task.tools and task.tools.agent else []
    for tool_name in enabled:
        raw_config = task.tools.agent.get(tool_name)
        if raw_config is None:
            continue
        if not isinstance(raw_config, dict):
            raise ValueError(f"tools.agent.{tool_name} must be a mapping")
        raw_source = raw_config.get("source")
        if raw_source is None:
            continue
        if not isinstance(raw_source, dict):
            raise ValueError(f"tools.agent.{tool_name}.source must be a mapping")
        declarations.append(
            (
                tool_name,
                validate_compose_source(
                    tool_name,
                    raw_source,
                    task_dir,
                    grading_method=grading_method,
                ),
            )
        )

    if grading_method == "test_execution" and not declarations:
        raise ValueError(
            "grading_method: test_execution requires an enabled agent tool with "
            "source.invocation_style: docker_compose_exec"
        )
    return declarations


def build_compose_tool_schema(
    *,
    tool_name: str,
    description: str,
    parameters: dict[str, Any],
    source: ComposeToolSourceConfig,
    toolset: str,
    task_dir_value: str,
    timeout_s: float,
) -> ToolSchema:
    """Construct the one lifecycle tool representation used by every adapter."""

    return ToolSchema(
        name=tool_name,
        description=description,
        parameters=parameters,
        category="compute",
        timeout_s=timeout_s,
        source=ToolSource(
            toolset=toolset,
            module_path="",
            class_name=tool_name,
            invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
            extra={
                "compose_file": source.compose_file,
                "task_dir": task_dir_value,
                "service": source.service,
                "tests_dir": source.tests_dir,
                "env_vars": dict(source.env_vars),
            },
        ),
    )


def bundle_compose_artifacts(
    task_dir: Path,
    *,
    compose_file: str,
    tests_dir: str,
    include_compose_context: bool,
) -> dict[str, str]:
    """Bundle compose, environment, and verifier artifacts with safe paths."""

    root = task_dir.resolve()
    compose_path = _resolve_task_path(root, compose_file, label="compose_file")
    tests_path = _resolve_task_path(root, tests_dir, label="tests_dir")
    if not compose_path.is_file():
        raise ValueError(f"Compose file not found: {compose_path}")
    if not tests_path.is_dir():
        raise ValueError(f"Tests directory not found: {tests_path}")

    files: set[Path] = {compose_path}
    files.update(path for path in tests_path.rglob("*") if path.is_file())
    if include_compose_context:
        context_root = compose_path.parent
        excluded_roots = {"solution", "golden", ".git"}
        files.update(
            path
            for path in context_root.rglob("*")
            if path.is_file()
            and not any(part in excluded_roots for part in path.relative_to(root).parts)
        )

    return {
        path.relative_to(root).as_posix(): base64.b64encode(path.read_bytes()).decode("ascii")
        for path in sorted(files)
    }
