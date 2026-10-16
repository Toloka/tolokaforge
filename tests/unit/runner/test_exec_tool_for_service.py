"""``exec_tool_for_service`` — the named-service resolution seam.

Running a verifier suite in a named compose service (the separate-verifier shape)
hard-requires a compose-backed, started executor. Unlike ``_exec_in_service``'s
agent-container fallback, a cross-container run cannot degrade, so a named service
that cannot be resolved must surface a CLEAR, actionable reason — never a bare
RuntimeError / AttributeError from reaching for a sibling resolver the tool does
not have. Both grading paths that take a ``service`` (the in-process run-suite
helper and the detached servicer) route refusals through this one seam.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.env_exec import SupportsServiceResolution, exec_tool_for_service
from tolokaforge.runner.models import ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    PersistentShellToolWrapper,
)

pytestmark = pytest.mark.unit


class _NonComposeExecTool:
    """Satisfies SupportsEnvExec but is not a DockerComposeExecToolWrapper."""

    def exec_in_env(self, command: str, timeout_s: float) -> str:  # noqa: ARG002
        return ""

    def exec_in_env_with_exit_code(self, command: str, timeout_s: float):  # noqa: ARG002
        return (0, "")


def _compose_wrapper(*, started: bool) -> DockerComposeExecToolWrapper:
    schema = ToolSchema(
        name="bash",
        description="stub",
        parameters={"type": "object", "properties": {}},
        category="compute",
        timeout_s=30.0,
        source=ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash",
            invocation_style="docker_compose_exec",
            extra={"service": "agent", "compose_project_prefix": "tbench_"},
        ),
    )
    wrapper = DockerComposeExecToolWrapper(
        schema, service="agent", compose_project_prefix="tbench_"
    )
    if started:
        wrapper._trial_id = "t-1"
        wrapper._container = "tbench_t-1_agent"
    return wrapper


def test_service_none_returns_the_agent_tool_unchanged() -> None:
    tool = _NonComposeExecTool()
    resolved, reason = exec_tool_for_service(tool, None)
    assert resolved is tool
    assert reason is None


def test_named_service_on_non_compose_tool_gives_clear_reason() -> None:
    resolved, reason = exec_tool_for_service(_NonComposeExecTool(), "verifier")
    assert resolved is None
    assert reason is not None
    assert "not a compose-backed executor" in reason
    assert "docker-compose runtime" in reason


def test_named_service_on_unstarted_compose_tool_gives_clear_reason() -> None:
    resolved, reason = exec_tool_for_service(_compose_wrapper(started=False), "verifier")
    assert resolved is None
    assert reason is not None
    assert "has not started" in reason


def test_named_service_on_started_compose_tool_resolves_sibling() -> None:
    resolved, reason = exec_tool_for_service(_compose_wrapper(started=True), "verifier")
    assert reason is None
    assert resolved is not None
    assert resolved.container_name == "tbench_t-1_verifier"


def _persistent_shell(*, service: str | None, started: bool) -> PersistentShellToolWrapper:
    """A session-lifetime shell on its compose backend (``service`` set) or local.

    The compose backend is exactly how the native builtin-dispatch path and the
    terminal-bench v4 route a compose-backed agent tool — a ``PersistentShell``,
    not the dedicated ``docker exec`` wrapper — so the sibling-resolution seam has
    to accept it too.
    """
    tool_config: dict[str, object] = {}
    if service is not None:
        tool_config = {"service": service, "compose_project_prefix": "tbench_"}
    schema = ToolSchema(
        name="bash_session",
        description="stub",
        parameters={"type": "object", "properties": {}},
        category="compute",
        timeout_s=30.0,
        tool_config=tool_config,
    )
    wrapper = PersistentShellToolWrapper(schema)
    if started:
        wrapper._trial_id = "t-1"
    return wrapper


def test_compose_backed_persistent_shell_satisfies_the_resolution_protocol() -> None:
    # The seam is structural, not a single concrete class: a compose-backed
    # session shell implements it just as the dedicated docker-exec wrapper does.
    assert isinstance(_persistent_shell(service="agent", started=True), SupportsServiceResolution)


def test_named_service_on_compose_persistent_shell_resolves_sibling() -> None:
    # The F5 fix: a compose-backed PersistentShell is no longer wrongly refused;
    # it resolves the sibling verifier container the same way the compose-exec
    # wrapper does, so a separate-verifier grade runs instead of booking ungradeable.
    resolved, reason = exec_tool_for_service(
        _persistent_shell(service="agent", started=True), "verifier"
    )
    assert reason is None
    assert resolved is not None
    assert resolved.container_name == "tbench_t-1_verifier"


def test_named_service_on_local_persistent_shell_gives_clear_reason() -> None:
    # A local (non-compose) shell has no sibling containers; the refusal is clear
    # and accurate rather than a bare RuntimeError.
    resolved, reason = exec_tool_for_service(
        _persistent_shell(service=None, started=True), "verifier"
    )
    assert resolved is None
    assert reason is not None
    assert "cannot be resolved" in reason
