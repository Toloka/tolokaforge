"""The factored engine-stack bring-up the orchestrator and library route share.

``build_engine_stack`` resolves the same service set + runner wiring the
single-box run derives, asserted on the ``EngineStack`` composition rather than
by starting Docker. ``run_trial_with_stack`` owns the stack lifecycle for one
trial: it starts the stack, points ``EXECUTOR_ADDRESS`` at the started runner,
runs the pure node entry, and tears the stack down — even when the trial raises.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.adapters.base import DockerStackRequirements
from tolokaforge.core.engine_stack_runtime import (
    EngineServiceConflictError,
    StartedEngineStack,
    build_engine_stack,
    resolve_engine_services,
    run_trial_with_stack,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.models.docker_config import DockerConfig, EngineServicesConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.docker.stacks.core import TypeSenseAddress

pytestmark = pytest.mark.unit

_LOGGER = get_logger("engine-stack-runtime-test", strict=False)


def _config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/unused"),
    )


def _task(enabled_tools: list[str] | None = None) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        initial_state={},
        tools={"agent": {"enabled": enabled_tools or []}, "user": {"enabled": []}},
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


def _runner(stack: Any):
    runner = stack.services.get("runner")
    assert runner is not None, "runner ServiceDefinition not found in stack"
    return runner


def test_build_engine_stack_builds_the_core_stack_for_a_stackless_task() -> None:
    """No manifest, no heavy tools: db-service + runner, no full-stack services."""
    stack, stack_requirements = build_engine_stack(
        _config(),
        None,
        [_task()],
        grpc_max_message_mb=64,
        state_read_timeout_s=12.5,
        logger=_LOGGER,
    )

    assert stack_requirements is None
    assert set(stack.services) == {"db-service", "runner"}
    assert "rag-service" not in stack.services
    assert "mock-web" not in stack.services


def test_build_engine_stack_threads_the_grpc_and_state_bounds_onto_the_runner() -> None:
    """``compute`` bounds the orchestrator passes reach the runner container."""
    stack, _ = build_engine_stack(
        _config(),
        None,
        [_task()],
        grpc_max_message_mb=77,
        state_read_timeout_s=9.0,
        logger=_LOGGER,
    )

    runner = _runner(stack)
    assert runner.environment["TOLOKAFORGE_GRPC_MAX_MESSAGE_MB"] == "77"
    assert runner.environment["TOLOKAFORGE_STATE_READ_TIMEOUT_S"] == "9.0"


def test_build_engine_stack_threads_the_search_plane_address() -> None:
    """A passed search-plane address lands on the runner, and on no other service."""
    stack, _ = build_engine_stack(
        _config(),
        None,
        [_task()],
        typesense_stack_kwargs={"typesense_address": TypeSenseAddress(host="typesense", port=8108)},
        grpc_max_message_mb=50,
        state_read_timeout_s=30.0,
        logger=_LOGGER,
    )

    carriers = sorted(
        name for name, svc in stack.services.items() if "TYPESENSE_HOST" in svc.environment
    )
    assert carriers == ["runner"]
    runner = _runner(stack)
    assert runner.environment["TYPESENSE_HOST"] == "typesense"
    assert runner.environment["TYPESENSE_PORT"] == "8108"


def test_build_engine_stack_with_no_search_plane_carries_no_address() -> None:
    """Absent kwargs == absent address: no service carries a stray plane."""
    stack, _ = build_engine_stack(
        _config(),
        None,
        [_task()],
        grpc_max_message_mb=50,
        state_read_timeout_s=30.0,
        logger=_LOGGER,
    )

    for svc in stack.services.values():
        assert "TYPESENSE_HOST" not in svc.environment


def _build(config: RunConfig, tasks: list[TaskConfig], adapter: Any = None) -> Any:
    stack, _ = build_engine_stack(
        config,
        adapter,
        tasks,
        grpc_max_message_mb=50,
        state_read_timeout_s=30.0,
        logger=_LOGGER,
    )
    return stack


def test_build_engine_stack_adds_mock_web_and_playwright_for_a_browser_task() -> None:
    """A browser task reaches mock-web and needs Playwright: mock-web + the build arg.

    It searches nothing, so rag-service is not in the stack.
    """
    stack = _build(_config(), [_task(["browser"])])

    assert set(stack.services) == {"db-service", "runner", "mock-web"}
    assert _runner(stack).build_args.get("INSTALL_PLAYWRIGHT") == "true"


def test_build_engine_stack_adds_rag_service_alone_for_a_search_task() -> None:
    stack = _build(_config(), [_task(["search_kb"])])

    assert set(stack.services) == {"db-service", "runner", "rag-service"}


def test_build_engine_stack_adds_both_when_the_tasks_need_both() -> None:
    stack = _build(_config(), [_task(["browser"]), _task(["search_kb"])])

    assert set(stack.services) == {"db-service", "runner", "rag-service", "mock-web"}


def test_build_engine_stack_adds_rag_service_alone_for_an_adapter_declared_need() -> None:
    adapter = SimpleNamespace(
        docker_stack_requirements=lambda: DockerStackRequirements(needs_rag_service=True)
    )

    stack = _build(_config(), [_task(["bash"])], adapter)

    assert set(stack.services) == {"db-service", "runner", "rag-service"}


def test_build_engine_stack_starts_a_service_the_config_says_always() -> None:
    config = _config().model_copy(
        update={"docker": DockerConfig(services=EngineServicesConfig(mock_web="always"))}
    )

    stack = _build(config, [_task(["bash"])])

    assert set(stack.services) == {"db-service", "runner", "mock-web"}


def test_build_engine_stack_refuses_a_never_service_the_run_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal comes before any stack is composed, let alone started."""

    def _no_stack(**kwargs: Any) -> Any:
        raise AssertionError("a stack factory ran despite the refusal")

    monkeypatch.setattr("tolokaforge.docker.stacks.core_stack", _no_stack)
    monkeypatch.setattr("tolokaforge.docker.stacks.full_stack", _no_stack)
    config = _config().model_copy(
        update={"docker": DockerConfig(services=EngineServicesConfig(rag_service="never"))}
    )

    with pytest.raises(EngineServiceConflictError, match="docker.services.rag_service"):
        _build(config, [_task(["search_kb"])])


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.records.append((message, fields))

    debug = warning = error = info


def test_build_engine_stack_without_optional_services_builds_the_core_stack_unchecked() -> None:
    """A run whose tasks declare their own stacks starts no built-in container.

    Its compose files alias the runner and db-service images only, so the
    optional services are neither built nor checked against ``docker.services``.
    """
    config = _config().model_copy(
        update={"docker": DockerConfig(services=EngineServicesConfig(mock_web="never"))}
    )

    logger = _RecordingLogger()

    stack, _ = build_engine_stack(
        config,
        None,
        [_task(["browser"])],
        grpc_max_message_mb=50,
        state_read_timeout_s=30.0,
        logger=logger,  # type: ignore[arg-type]
        optional_services=False,
    )

    assert set(stack.services) == {"db-service", "runner"}
    assert dict(logger.records)[
        "docker.services is ignored: the tasks declare their own stacks"
    ] == {
        "rag_service": "auto",
        "mock_web": "never",
    }


@pytest.mark.parametrize(
    ("manifest", "services"),
    [(None, {"db-service", "runner", "mock-web"}), ("environment.yaml", {"db-service", "runner"})],
    ids=["built-in stack", "task-declared stack"],
)
def test_the_orchestrator_builds_optional_services_only_for_the_built_in_stack(
    monkeypatch: pytest.MonkeyPatch, manifest: str | None, services: set[str]
) -> None:
    """A task-declared stack skips the optional services, and with them ``docker.services``.

    The built-in stack starts mock-web for the browser task and rag-service for
    ``always``; tasks that bring their own stack start no built-in container, so
    the run builds neither image.
    """
    config = _config().model_copy(
        update={"docker": DockerConfig(services=EngineServicesConfig(rag_service="always"))}
    )
    orchestrator = Orchestrator(config)
    orchestrator.tasks = [_task(["browser"])]
    orchestrator.adapter = SimpleNamespace(  # type: ignore[assignment]
        docker_stack_requirements=lambda: DockerStackRequirements()
    )
    monkeypatch.setattr(
        orchestrator,
        "_task_description",
        lambda task_id: SimpleNamespace(environment_manifest=manifest),
    )

    stack, _, task_stack_mode = orchestrator._build_run_engine_stack()

    assert task_stack_mode is (manifest is not None)
    assert set(stack.services) == services | ({"rag-service"} if manifest is None else set())


def test_build_engine_stack_logs_each_service_and_why() -> None:
    """The line the run log points operators to names each decision and its reason."""
    logger = _RecordingLogger()

    build_engine_stack(
        _config(),
        None,
        [_task(["browser"])],
        grpc_max_message_mb=50,
        state_read_timeout_s=30.0,
        logger=logger,  # type: ignore[arg-type]
    )

    plan = resolve_engine_services([_task(["browser"])], None, EngineServicesConfig())
    assert dict(logger.records)["Engine services resolved"] == {
        "rag_service": plan.rag_service.start,
        "rag_service_reason": plan.rag_service.reason,
        "mock_web": plan.mock_web.start,
        "mock_web_reason": plan.mock_web.reason,
    }


class _Recorder:
    """Records the bring-up / run / teardown order and the env seen mid-run."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.service_stack = object()
        self.runner_address = "127.0.0.1:54321"

    def start_engine_stack(self, *args: Any, **kwargs: Any) -> StartedEngineStack:
        self.calls.append(("start", kwargs.get("grpc_max_message_mb")))
        return StartedEngineStack(
            service_stack=self.service_stack,
            runner_address=self.runner_address,
            stack_requirements=None,
        )

    def teardown_engine_stack(self, service_stack: Any, **kwargs: Any) -> None:
        self.calls.append(("teardown", service_stack is self.service_stack))


def _inputs(grpc: int = 42) -> Any:
    return SimpleNamespace(
        task=SimpleNamespace(task_id="t"),
        task_pack_dir=None,
        grpc_max_message_mb=grpc,
        trial_spec=SimpleNamespace(run_id="run-1"),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, rec: _Recorder, run_impl: Any) -> None:
    monkeypatch.setattr(
        "tolokaforge.core.engine_stack_runtime.start_engine_stack", rec.start_engine_stack
    )
    monkeypatch.setattr(
        "tolokaforge.core.engine_stack_runtime.teardown_engine_stack", rec.teardown_engine_stack
    )
    monkeypatch.setattr("tolokaforge.core.trial_node.run_trial_from_inputs", run_impl)


def test_run_trial_with_stack_starts_points_runs_and_tears_down_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    sentinel = object()

    def _run(inputs: Any) -> Any:
        rec.calls.append(("run", os.environ.get("EXECUTOR_ADDRESS")))
        return sentinel

    _wire(monkeypatch, rec, _run)
    monkeypatch.delenv("EXECUTOR_ADDRESS", raising=False)

    result = run_trial_with_stack(_inputs(grpc=42), _config(), adapter=object())

    assert result is sentinel
    # Start first (carrying the trial's own gRPC bound), then run while
    # EXECUTOR_ADDRESS names the started runner, then tear the stack down.
    assert rec.calls == [
        ("start", 42),
        ("run", "127.0.0.1:54321"),
        ("teardown", True),
    ]
    # The env is left as it was found.
    assert "EXECUTOR_ADDRESS" not in os.environ


def test_run_trial_with_stack_tears_down_and_restores_env_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()

    def _run(inputs: Any) -> Any:
        rec.calls.append(("run", os.environ.get("EXECUTOR_ADDRESS")))
        raise RuntimeError("trial blew up")

    _wire(monkeypatch, rec, _run)
    monkeypatch.setenv("EXECUTOR_ADDRESS", "prior:9999")

    with pytest.raises(RuntimeError, match="trial blew up"):
        run_trial_with_stack(_inputs(), _config(), adapter=object())

    # Teardown ran despite the failure, and the prior address is restored.
    assert ("teardown", True) in rec.calls
    assert rec.calls[-1] == ("teardown", True)
    assert os.environ["EXECUTOR_ADDRESS"] == "prior:9999"
