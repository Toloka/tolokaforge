"""Orchestrator bakes the docker CLI into the runner image when the run
needs to ``docker exec`` from inside the runner container. Two triggers
today: the terminal-bench adapter (which shells out to docker directly
against the host daemon via the mounted socket) and any task whose
enabled ``bash_session`` / ``str_replace_editor`` uses the compose
variant (``tools.agent.<tool>.service: <name>`` — the Migration Bench
adapter shape). Every other run builds the slim default image without
the CLI (#539).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.orchestrator import _run_needs_docker_cli, _tasks_use_compose_variant_tools
from tolokaforge.runner.models import AdapterType

pytestmark = pytest.mark.unit


class _StubTools:
    """Minimal ToolsConfig stand-in: exposes an ``agent`` dict."""

    def __init__(self, agent: dict) -> None:
        self.agent = agent


class _StubTask:
    def __init__(self, tools: _StubTools | None) -> None:
        self.tools = tools


# ---- _run_needs_docker_cli: adapter-type trigger ----


def test_terminal_bench_string_triggers_docker_cli():
    assert _run_needs_docker_cli("terminal_bench", tasks=[]) is True


def test_terminal_bench_enum_triggers_docker_cli():
    assert _run_needs_docker_cli(AdapterType.TERMINAL_BENCH, tasks=[]) is True


def test_native_adapter_without_compose_tools_returns_false():
    assert _run_needs_docker_cli("native", tasks=[]) is False


def test_other_adapter_without_compose_tools_returns_false():
    assert _run_needs_docker_cli(AdapterType.TAU, tasks=[]) is False


def test_no_adapter_without_compose_tools_returns_false():
    assert _run_needs_docker_cli(None, tasks=[]) is False


# ---- _tasks_use_compose_variant_tools: compose-variant trigger ----


def test_tasks_with_bash_session_compose_variant_trigger():
    """``bash_session.service: <name>`` = the compose variant.
    Runner must docker-exec into the sibling service."""
    task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["bash_session"],
                "bash_session": {"service": "mb-server", "compose_project_prefix": "env_"},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([task]) is True


def test_tasks_with_str_replace_editor_compose_variant_trigger():
    task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["str_replace_editor"],
                "str_replace_editor": {"service": "mb-server", "working_root": "/workdir"},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([task]) is True


def test_tasks_with_both_compose_variants_trigger():
    """MB adapter's shape — both compose variants enabled."""
    task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["bash_session", "str_replace_editor"],
                "bash_session": {"service": "mb-server"},
                "str_replace_editor": {"service": "mb-server", "working_root": "/workdir"},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([task]) is True


def test_enabled_bash_session_without_service_does_not_trigger():
    """Local variant (no ``service:``) runs inside the runner; no
    docker exec needed."""
    task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["bash_session"],
                "bash_session": {"timeout_s": 60},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([task]) is False


def test_bash_session_config_without_being_enabled_does_not_trigger():
    """A stale ``bash_session:`` block in the config that isn't in
    ``enabled`` should not fire the CLI install."""
    task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": [],
                "bash_session": {"service": "mb-server"},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([task]) is False


def test_no_tools_config_does_not_trigger():
    task = _StubTask(tools=None)
    assert _tasks_use_compose_variant_tools([task]) is False


def test_empty_task_list_does_not_trigger():
    assert _tasks_use_compose_variant_tools([]) is False


def test_mixed_tasks_trigger_if_any_uses_compose_variant():
    """Positive detection over a task list — any single compose-variant
    task drives the runner image choice for the whole run."""
    plain_task = _StubTask(
        tools=_StubTools(agent={"enabled": ["bash_session"], "bash_session": {}})
    )
    compose_task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["bash_session"],
                "bash_session": {"service": "mb-server"},
            }
        )
    )
    assert _tasks_use_compose_variant_tools([plain_task, compose_task]) is True


# ---- Composition: native adapter + compose-variant tools = docker CLI needed ----


def test_native_adapter_with_compose_variant_tools_triggers_docker_cli():
    """The Migration Bench adapter case: native adapter type + compose-variant
    tool routing — was the missing case that made #841's readiness-gate + DB-gate
    fixes surface a third-layer failure (`Tool lifecycle start failed: No such
    file or directory: 'docker'`)."""
    mb_task = _StubTask(
        tools=_StubTools(
            agent={
                "enabled": ["bash_session", "str_replace_editor"],
                "bash_session": {"service": "mb-server"},
                "str_replace_editor": {"service": "mb-server", "working_root": "/workdir"},
            }
        )
    )
    assert _run_needs_docker_cli("native", tasks=[mb_task]) is True
