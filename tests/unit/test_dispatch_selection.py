"""Dispatch-axis selection, the ``runtime='remote'`` alias, and neutral-vocab routing.

* ``orchestrator.dispatch`` resolves to its adapter; ``runtime='remote'`` is a
  deprecated alias mapping onto ``grpc_node`` (with a ``DeprecationWarning``).
* ``build_trial_execution_unit`` selects :class:`RemoteTrialExecutor` whenever a
  dispatch adapter is present, and builds no local conductor or grader.
* The executor drives on the transport-neutral dispatch errors alone: a
  :class:`DispatchRefused` / :class:`DispatchProvisionError` becomes a
  non-retryable ``PROVISION_ERROR`` result; a :class:`DispatchTransient`
  propagates for the orchestrator to retry.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.canonical._factories import make_task_config, make_trial_spec
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
)
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    GRPC_NODE_DISPATCH_ADAPTER,
    DispatchConfig,
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_executor import RemoteTrialExecutor
from tolokaforge.core.trial_node import (
    TrialExecutionContext,
    build_trial_execution_unit,
)

pytestmark = pytest.mark.unit

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


def _config(**orchestrator: object) -> RunConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return RunConfig(
            models={"agent": _AGENT, "user": _USER},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, **orchestrator),
            evaluation=EvaluationConfig(output_dir="/tmp/unused"),
            compute={"grpc_max_message_mb": 9},
        )


# ---- Dispatch-config resolution + the deprecated alias ---------------------


def test_an_explicit_dispatch_block_resolves_without_a_warning() -> None:
    config = _config(dispatch=DispatchConfig(adapter="grpc_node", config={"nodes": ["a:1"]}))
    orch = Orchestrator(config)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        resolved = orch._dispatch_config()

    assert resolved is not None
    assert resolved.adapter == "grpc_node"
    assert orch._is_dispatch_run()
    assert orch._dispatches_via_grpc_node()


def test_runtime_remote_is_a_deprecated_alias_mapping_to_grpc_node() -> None:
    config = _config(runtime="remote", remote={"nodes": ["a:1", "b:2"], "lease_timeout_s": 5})
    orch = Orchestrator(config)

    with pytest.warns(DeprecationWarning, match="deprecated alias"):
        resolved = orch._dispatch_config()

    assert resolved is not None
    assert resolved.adapter == GRPC_NODE_DISPATCH_ADAPTER
    assert resolved.config["nodes"] == ["a:1", "b:2"]
    assert orch._dispatches_via_grpc_node()


def test_no_dispatch_block_and_no_alias_is_a_single_box_run() -> None:
    orch = Orchestrator(_config())
    assert orch._dispatch_config() is None
    assert not orch._is_dispatch_run()


def test_build_dispatch_adapter_injects_the_runs_grpc_message_bound() -> None:
    config = _config(dispatch=DispatchConfig(adapter="grpc_node", config={"nodes": ["a:1"]}))
    orch = Orchestrator(config)

    adapter = orch._build_dispatch_adapter(orch._dispatch_config())

    assert isinstance(adapter, GrpcNodeDispatchAdapter)
    assert adapter.grpc_max_message_mb == 9  # from compute.grpc_max_message_mb


def test_a_bad_dispatch_adapter_name_fails_loud() -> None:
    config = _config(dispatch=DispatchConfig(adapter="nope", config={}))
    orch = Orchestrator(config)

    with pytest.raises(UnknownImplementationError):
        orch._build_dispatch_adapter(orch._dispatch_config())


def test_runtime_remote_and_dispatch_block_together_is_refused() -> None:
    with (
        warnings.catch_warnings(),
        pytest.raises(ValueError, match="set one, not both"),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        OrchestratorConfig(
            runtime="remote",
            remote={"nodes": ["a:1"]},
            dispatch=DispatchConfig(adapter="grpc_node", config={"nodes": ["a:1"]}),
        )


# ---- build_trial_execution_unit selection ---------------------------------


class _FakeAdapter:
    """A :class:`DispatchAdapter` whose ``run_trial_node`` raises *exc* (or returns *result*)."""

    def __init__(self, *, exc: Exception | None = None, result: TrialResult | None = None) -> None:
        self._exc = exc
        self._result = result

    def connect(self) -> None:  # pragma: no cover - never reached in these tests
        pass

    def close(self) -> None:  # pragma: no cover - never reached in these tests
        pass

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _dispatch_unit(tmp_path: Path, adapter: _FakeAdapter):
    unit = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=SimpleNamespace(trial_grader_name="runner_rpc"),  # type: ignore[arg-type]
            artifact_writer=InMemoryArtifactWriter(),
            config=_config(),
            logger=get_logger("dispatch-selection", strict=False),
            agent_client=None,  # type: ignore[arg-type]
            runtime_backend=None,
            dispatch_adapter=adapter,  # type: ignore[arg-type]
            output_dir=tmp_path,
            request_limiter=None,
            grader_config=None,
            grpc_max_message_mb=9,
        )
    )
    return unit


def test_a_dispatch_adapter_selects_the_remote_executor_and_builds_no_conductor_or_grader(
    tmp_path: Path,
) -> None:
    unit = _dispatch_unit(tmp_path, _FakeAdapter(result=_success()))

    assert isinstance(unit.executor, RemoteTrialExecutor)
    assert unit.conductor is None
    assert unit.grader is None
    unit.close()  # no grader to close — must not raise


def _success(trial_id: str = "task-1:0") -> TrialResult:
    from tolokaforge.core.conductor import _default_success_trajectory

    return TrialResult.from_trajectory(
        trial_id=trial_id, trajectory=_default_success_trajectory("task-1", 0)
    )


def _spec() -> TrialSpec:
    return make_trial_spec(trial_id="task-1:0", task_id="task-1")


def _task() -> TaskConfig:
    return make_task_config(task_id="task-1")


# ---- Neutral-vocab routing through the executor ----------------------------


@pytest.mark.parametrize(
    "error",
    [
        DispatchRefused("TaskPackDrift", "task_pack", "pack drifted"),
        DispatchProvisionError("ProvisionError", "await_ready", "db never healthy"),
    ],
)
def test_a_refusal_or_provision_error_becomes_a_non_retryable_provision_failure(
    tmp_path: Path, error: Exception
) -> None:
    unit = _dispatch_unit(tmp_path, _FakeAdapter(exc=error))

    result = unit.execute(_spec(), _task())

    assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR
    assert result.trajectory.grade is None
    assert not Orchestrator._is_retryable_trajectory(result.trajectory)


def test_a_dispatch_transient_propagates_for_the_orchestrator_to_retry(tmp_path: Path) -> None:
    unit = _dispatch_unit(
        tmp_path, _FakeAdapter(exc=DispatchTransient("Internal", "", "node busy"))
    )

    with pytest.raises(DispatchTransient, match="node busy"):
        unit.execute(_spec(), _task())


def test_the_provision_error_stage_and_reason_follow_the_neutral_error(tmp_path: Path) -> None:
    unit = _dispatch_unit(
        tmp_path,
        _FakeAdapter(
            exc=DispatchProvisionError("ProvisionError", "await_ready", "db never healthy")
        ),
    )

    result = unit.execute(_spec(), _task())

    # A provision error keeps the node's stage; its reason carries no error_type prefix.
    assert result.trajectory.provision_stage == "await_ready"


# ---- Observability under-warning: any dispatch run, not just grpc_node ------


def _config_with_tracing(exporter: str, **orchestrator: object) -> RunConfig:
    from tolokaforge.core.models.run_config import ObservabilityConfig, TracingConfig

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return RunConfig(
            models={"agent": _AGENT, "user": _USER},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, **orchestrator),
            evaluation=EvaluationConfig(output_dir="/tmp/unused"),
            compute={"grpc_max_message_mb": 9},
            observability=ObservabilityConfig(tracing=TracingConfig(exporter=exporter)),
        )


def test_a_local_dispatch_run_with_tracing_warns_the_node_carries_no_observer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config_with_tracing("otlp", dispatch=DispatchConfig(adapter="local", config={}))
    orch = Orchestrator(config)
    assert orch._is_dispatch_run()
    assert not orch._dispatches_via_grpc_node()

    with caplog.at_level(logging.WARNING):
        orch._refuse_unsupported_dispatch_run()

    warnings_logged = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no live traces" in message for message in warnings_logged)


def test_a_local_dispatch_run_without_tracing_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config_with_tracing("none", dispatch=DispatchConfig(adapter="local", config={}))
    orch = Orchestrator(config)

    with caplog.at_level(logging.WARNING):
        orch._refuse_unsupported_dispatch_run()

    warnings_logged = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("no live traces" in message for message in warnings_logged)
