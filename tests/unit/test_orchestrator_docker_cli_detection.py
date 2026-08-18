"""Orchestrator bakes the docker CLI into the runner image when the run
needs to ``docker exec`` from inside the runner container. Two triggers
today: the terminal-bench adapter (which shells out to docker directly
against the host daemon via the mounted socket) and any task whose
enabled ``bash_session`` / ``str_replace_editor`` uses the compose
variant (``tools.agent.<tool>.service: <name>`` — the Migration Bench
adapter shape). Every other run builds the slim default image without
the CLI (#539). The same predicate wires ``mount_docker_socket`` on the
runtime backend build context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    HarnessAdapterConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import (
    Orchestrator,
    _run_needs_docker_cli,
    _tasks_use_compose_variant_tools,
)
from tolokaforge.runner.models import AdapterType

pytestmark = pytest.mark.unit


class _StubTools:
    """Minimal ToolsConfig stand-in: exposes an ``agent`` and a ``user`` dict."""

    def __init__(self, agent: dict, user: dict | None = None) -> None:
        self.agent = agent
        self.user = user if user is not None else {"enabled": []}


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


def test_a_user_declared_compose_variant_triggers_and_reads_its_own_block():
    """The ``service:`` key is read from the block that enabled the tool.

    A user-declared ``bash_session`` needs the docker CLI for the same reason the
    agent's does. The second task is the discriminating half: the agent's block
    names a service for a tool the agent does not enable, and the user's block
    enables it without one — so a reader crossing the two blocks would fire on a
    task that routes nothing.
    """
    routed = _StubTask(
        tools=_StubTools(
            agent={"enabled": []},
            user={"enabled": ["bash_session"], "bash_session": {"service": "mb-server"}},
        )
    )
    crossed = _StubTask(
        tools=_StubTools(
            agent={"enabled": [], "bash_session": {"service": "mb-server"}},
            user={"enabled": ["bash_session"], "bash_session": {"timeout_s": 60}},
        )
    )

    assert _tasks_use_compose_variant_tools([routed]) is True
    assert _tasks_use_compose_variant_tools([crossed]) is False


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


# ---- _construct_runtime_backend wires the socket flag through the same predicate ----


def _run_config(*, adapter_type: str | None) -> RunConfig:
    """Build a minimal RunConfig, optionally pinning the harness adapter type."""
    harness_adapter = (
        HarnessAdapterConfig(type=adapter_type, params={}) if adapter_type is not None else None
    )
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output", harness_adapter=harness_adapter),
    )


def _capture_build_context(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch the runtime-backend loader to intercept the context the factory
    receives. The returned list is populated in-order with each context; the
    factory returns a MagicMock so the orchestrator method returns cleanly."""
    captured: list[Any] = []

    def _fake_factory(ctx: Any) -> Any:
        captured.append(ctx)
        return MagicMock(name="RuntimeBackend")

    monkeypatch.setattr(
        "tolokaforge.core.orchestrator.load_runtime_backend",
        lambda _name: _fake_factory,
    )
    return captured


class TestConstructRuntimeBackendMountSocket:
    """The build context the orchestrator hands the runtime-backend factory
    must carry ``mount_docker_socket`` derived from the *same* predicate that
    decides whether to bake the docker CLI into the runner image. Diverging
    the two produces an image with the CLI and no socket, or vice versa —
    either way the runner cannot ``docker exec`` at trial time."""

    def _build_orchestrator(
        self, monkeypatch: pytest.MonkeyPatch, *, adapter_type: str
    ) -> tuple[Orchestrator, list[Any]]:
        """A minimal Orchestrator whose task-driven backend selection resolves
        to ``shared`` (no environment_manifest) and whose runtime-backend
        loader is stubbed to capture the build context."""
        from tests.canonical._factories import make_task_description

        captured = _capture_build_context(monkeypatch)
        orch = Orchestrator(_run_config(adapter_type=adapter_type))
        task = MagicMock()
        task.task_id = "t1"
        task.tools = None
        orch.tasks = [task]
        orch.adapter = MagicMock()
        orch.adapter.to_task_description.side_effect = lambda tid: make_task_description(
            task_id=tid
        )
        return orch, captured

    def test_terminal_bench_run_with_no_compose_variant_tools_mounts_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, captured = self._build_orchestrator(
            monkeypatch, adapter_type=AdapterType.TERMINAL_BENCH
        )

        orch._construct_runtime_backend(runner_address="sentinel:50051")

        assert len(captured) == 1
        assert captured[0].mount_docker_socket is True

    def test_native_run_with_no_compose_variant_tools_does_not_mount_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch, captured = self._build_orchestrator(monkeypatch, adapter_type=AdapterType.NATIVE)

        orch._construct_runtime_backend(runner_address="sentinel:50051")

        assert len(captured) == 1
        assert captured[0].mount_docker_socket is False


# ---- DockerStackRequirements.image_builds carve-out from to_core_stack_kwargs ----


class TestDockerStackRequirementsImageBuildsCarveOut:
    """``image_builds`` is the orchestrator's declarative pre-build seam, not
    a stack kwarg. It must be omitted from ``to_core_stack_kwargs()`` for the
    same reason ``needs_rag_service`` is: neither ``core_stack`` nor
    ``full_stack`` accepts it."""

    def test_image_builds_absent_from_kwargs(self, tmp_path: Path) -> None:
        from tolokaforge.adapters.base import ComposeImageBuild, DockerStackRequirements

        compose = tmp_path / "docker-compose.yaml"
        compose.write_text("services:\n  main:\n    image: example:local\n")

        requirements = DockerStackRequirements(
            image_builds=[ComposeImageBuild(compose_file=compose, service="main")],
        )

        assert requirements.to_core_stack_kwargs() == {}

    def test_image_builds_default_is_empty_list(self) -> None:
        from tolokaforge.adapters.base import DockerStackRequirements

        assert DockerStackRequirements().image_builds == []


# ---- Orchestrator invokes docker compose build once per declared image ----


class TestPerformDeclaredComposeImageBuilds:
    """Adapter-declared compose images build once at run start, outside the
    trial path. Empty ``image_builds`` invokes nothing; each declared build
    invokes ``docker compose -f <file> build <service>`` exactly once, so a
    broken Dockerfile aborts the run at prep time (raise-and-abort) instead
    of surfacing as a per-trial ``PROVISION_ERROR`` naming compose."""

    def test_empty_image_builds_invokes_no_subprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tolokaforge.adapters.base import DockerStackRequirements

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kwargs: calls.append(cmd) or MagicMock(returncode=0),
        )

        orch = Orchestrator(_run_config(adapter_type=None))
        orch._perform_declared_compose_image_builds(DockerStackRequirements())

        assert calls == []

    def test_none_stack_requirements_invokes_no_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kwargs: calls.append(cmd) or MagicMock(returncode=0),
        )

        orch = Orchestrator(_run_config(adapter_type=None))
        orch._perform_declared_compose_image_builds(None)

        assert calls == []

    def test_one_declared_build_invokes_docker_compose_build_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tolokaforge.adapters.base import ComposeImageBuild, DockerStackRequirements

        compose = tmp_path / "docker-compose.yaml"
        compose.write_text("services:\n  main:\n    image: missing-image:local\n")

        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
            calls.append(list(cmd))
            # ``docker image inspect`` for the pinned image reports "not present"
            # so the pre-build helper does not short-circuit; the ``docker compose
            # build`` call then succeeds. Any other command is unexpected.
            if cmd[:3] == ["docker", "image", "inspect"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", _fake_run)

        orch = Orchestrator(_run_config(adapter_type=None))
        orch._perform_declared_compose_image_builds(
            DockerStackRequirements(
                image_builds=[ComposeImageBuild(compose_file=compose, service="main")],
            )
        )

        build_calls = [cmd for cmd in calls if cmd[:2] == ["docker", "compose"] and "build" in cmd]
        assert build_calls == [["docker", "compose", "-f", str(compose), "build", "main"]]
