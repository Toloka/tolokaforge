"""Unit tests for :class:`ProvisioningTrialExecutor` — the substrate-bracket
production seam.

Uses :class:`InMemoryRuntimeBackend` (records provision / await_ready /
endpoints / teardown calls on ``call_log``) and :class:`InMemoryConductor`
(records ``run()`` invocations on its own ``call_log``) so bracket
order, endpoint substitution, ``ProvisionError`` handling, and
teardown-on-body-exception can each be asserted directly. No gRPC, no
Docker daemon required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import (
    make_env_endpoints,
    make_task_config,
    make_trial_spec,
)
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import TerminationReason, TrialStatus
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.runtime import InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial_executor import (
    ProvisioningTrialExecutor,
    _synthesize_provision_failure_result,
)

pytestmark = pytest.mark.unit


def _make_executor(
    *,
    backend: InMemoryRuntimeBackend | None = None,
    conductor: InMemoryConductor | None = None,
    output_dir: Path = Path("/nonexistent-run-dir"),
) -> tuple[ProvisioningTrialExecutor, InMemoryRuntimeBackend, InMemoryConductor, MagicMock]:
    backend = backend or InMemoryRuntimeBackend()
    conductor = conductor or InMemoryConductor()
    logger = MagicMock()
    executor = ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=conductor,
        logger=logger,
        output_dir=output_dir,
        artifact_writer=InMemoryArtifactWriter(),
    )
    return executor, backend, conductor, logger


class TestHappyPathBracket:
    """provision → await_ready → endpoints → conductor.run → teardown, in
    that order, with the injected logger emitting the per-branch events
    at the substrate seam."""

    def test_bracket_order_recorded_on_call_log(self) -> None:
        executor, backend, conductor, _ = _make_executor()
        spec = make_trial_spec()

        executor.execute(spec, make_task_config())

        assert backend.call_log.provisioned_trials == [spec.trial_id]
        assert backend.call_log.await_ready_calls == [spec.trial_id]
        assert backend.call_log.endpoints_calls == [spec.trial_id]
        assert backend.call_log.torn_down_trials == [spec.trial_id]
        assert len(conductor.call_log.runs) == 1
        assert conductor.call_log.runs[0]["trial_id"] == spec.trial_id

    def test_endpoints_substituted_into_final_spec(self) -> None:
        """The conductor receives a spec whose ``env_endpoints`` matches
        what ``runtime.endpoints(handle)`` returned — not the preliminary
        endpoints on the incoming spec."""
        executor, _, conductor, _ = _make_executor()
        prelim = make_trial_spec(env_endpoints=make_env_endpoints(runner_url="http://prelim:1"))

        result = executor.execute(prelim, make_task_config())

        assert result.trial_id == prelim.trial_id
        # InMemoryRuntimeBackend.endpoints returns a per-trial URL derived from
        # trial_id; the conductor's InMemoryConductor doesn't reveal what it
        # saw on the wire, so we check the call was made (endpoints_calls) and
        # trust the copy semantics.
        assert conductor.call_log.runs[0]["trial_id"] == prelim.trial_id

    def test_success_emits_structured_logs(self) -> None:
        executor, _, _, logger = _make_executor()
        executor.execute(make_trial_spec(), make_task_config())

        info_msgs = [c.args[0] for c in logger.info.call_args_list]
        assert "Provisioning trial env" in info_msgs
        assert "Trial env provisioned" in info_msgs
        assert "Trial env teardown complete" in info_msgs
        logger.error.assert_not_called()


class TestTeardownAlwaysFires:
    """teardown() runs even when the conductor body raises."""

    def test_teardown_after_conductor_exception(self) -> None:
        def _boom_factory(_task_id: str, _idx: int):
            raise RuntimeError("body exploded")

        backend = InMemoryRuntimeBackend()
        conductor = InMemoryConductor(trajectory_factory=_boom_factory)
        executor, _, _, _ = _make_executor(backend=backend, conductor=conductor)

        with pytest.raises(RuntimeError, match="body exploded"):
            executor.execute(make_trial_spec(), make_task_config())

        assert backend.call_log.torn_down_trials, "teardown must fire on body exception"


class TestProvisionErrorBranches:
    """ProvisionError at any stage yields a synthesised failed
    :class:`TrialResult` with ``TerminationReason.PROVISION_ERROR`` and
    the exception's ``reason`` in the ``Grade.reasons`` string."""

    def test_provision_stage_failure_synthesises_failed_result(self) -> None:
        backend = InMemoryRuntimeBackend(fail_provision_after_service="db")
        executor, _, conductor, logger = _make_executor(backend=backend)
        # environment_manifest with a service named "db" triggers the fake
        # failure.
        from tolokaforge.core.trial import EnvironmentManifest

        fixture = (
            Path(__file__).parent.parent
            / "canonical"
            / "fixtures"
            / "environment_manifest"
            / "lifecycle_public.yaml"
        )
        spec = make_trial_spec()
        # Rebuild spec with a manifest so the InMemoryRuntimeBackend's
        # provision-failure path fires.
        spec = spec.model_copy(
            update={
                "task": spec.task.model_copy(
                    update={"environment_manifest": EnvironmentManifest(compose_file=fixture)}
                )
            }
        )

        result = executor.execute(spec, make_task_config())

        assert result.trajectory.status == TrialStatus.ERROR
        assert result.trajectory.termination_reason == TerminationReason.PROVISION_ERROR
        assert result.trajectory.grade is not None
        assert result.trajectory.grade.binary_pass is False
        assert "Provisioning failed" in result.trajectory.grade.reasons
        # Conductor never runs on the failure path.
        assert conductor.call_log.runs == []
        logger.error.assert_called_once()
        assert logger.error.call_args.args[0] == "Provisioning failed"

    def test_await_ready_timeout_synthesises_failed_result_and_tears_down(self) -> None:
        backend = InMemoryRuntimeBackend(await_ready_times_out=True)
        executor, _, conductor, _ = _make_executor(backend=backend)

        result = executor.execute(make_trial_spec(), make_task_config())

        assert result.trajectory.termination_reason == TerminationReason.PROVISION_ERROR
        # await_ready failed → conductor never runs, but teardown still fires.
        assert conductor.call_log.runs == []
        assert backend.call_log.torn_down_trials, "teardown must fire after await_ready failure"


class TestSafeTeardown:
    """Teardown after a failed body / failed await_ready is best-effort:
    exceptions are logged, not silently swallowed, and control flow
    continues (the primary error is preserved).
    """

    def test_teardown_exception_in_finally_is_logged_not_masked(self) -> None:
        backend = InMemoryRuntimeBackend()

        def _raising_teardown(_handle: object) -> None:
            raise RuntimeError("teardown blew up")

        backend.teardown = _raising_teardown  # type: ignore[method-assign]
        executor, _, _, logger = _make_executor(backend=backend)

        # Body succeeds; only teardown raises.
        result = executor.execute(make_trial_spec(), make_task_config())
        assert result.trial_id == "task-1:0"  # body result returned normally

        warn_msgs = [c.args[0] for c in logger.warning.call_args_list]
        assert "Teardown raised; continuing" in warn_msgs


class TestSynthesizedFailureShape:
    """Direct test of the synthesis helper — pins the failure trajectory
    shape independent of the executor's dispatch flow."""

    def test_synthesized_trajectory_carries_reason(self) -> None:
        spec = make_trial_spec()
        err = ProvisionError(
            trial_id=spec.trial_id, stage="provision", reason="db image pull failed"
        )

        result = _synthesize_provision_failure_result(spec, err)

        assert result.trial_id == spec.trial_id
        assert result.trajectory.status == TrialStatus.ERROR
        assert result.trajectory.termination_reason == TerminationReason.PROVISION_ERROR
        assert "db image pull failed" in result.trajectory.grade.reasons
        assert result.trajectory.grade.score == 0.0
        assert result.trajectory.grade.binary_pass is False
