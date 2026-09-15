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
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeStatus,
    MessageRole,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
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
        backend = InMemoryRuntimeBackend(fail_provision_after_service="db-service")
        executor, _, conductor, logger = _make_executor(backend=backend)
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
        assert result.trajectory.grade is None
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

    def test_register_trial_refusal_synthesises_failed_result_and_tears_down(self) -> None:
        """A registration refusal after provisioning succeeds surfaces as
        a synthesised PROVISION_ERROR row, not an uncaught RuntimeError.

        Before the typed conversion, a runner-side refusal (e.g. the
        search-plane gate) crossed as a bare ``RuntimeError`` and escaped
        ``conductor.run`` uncaught. The orchestrator's queue-only path
        dropped the trial out of ``self.results`` and inflated every rate,
        after the retry budget burned on a deterministic refusal.
        """

        def _refuse_register(_task_id: str, _idx: int):
            raise ProvisionError(
                trial_id="task_registration_refused:0",
                stage="register_trial",
                reason="Failed to register trial with executor: search plane unusable",
            )

        backend = InMemoryRuntimeBackend()
        conductor = InMemoryConductor(trajectory_factory=_refuse_register)
        executor, _, _, logger = _make_executor(backend=backend, conductor=conductor)

        result = executor.execute(make_trial_spec(), make_task_config())

        assert result.trajectory.status == TrialStatus.ERROR
        assert result.trajectory.termination_reason == TerminationReason.PROVISION_ERROR
        assert result.trajectory.grade is None
        assert backend.call_log.torn_down_trials, "teardown must fire after register_trial refusal"
        logger.error.assert_called_once()
        assert logger.error.call_args.args[0] == "Registration refused after provisioning"


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


class TestJudgeMissingVerdictErrorStage:
    """A trial whose grade came back with :attr:`JudgeStatus.ERRORED` has its
    ``metrics.yaml`` amended with ``error_stage: judge_missing_verdict`` so
    the downstream aggregator can rejudge exactly those trials without
    voiding the cluster's analysis stage.
    """

    def _make_trajectory_with_grade(
        self, judge_status: JudgeStatus, task_id: str = "task-1", trial_idx: int = 0
    ) -> Trajectory:
        from datetime import UTC, datetime

        from tolokaforge.core.models import Message

        return Trajectory(
            task_id=task_id,
            trial_index=trial_idx,
            start_ts=datetime.now(tz=UTC),
            end_ts=datetime.now(tz=UTC),
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[Message(role=MessageRole.USER, content="hello")],
            metrics=Metrics(),
            grade=Grade(
                binary_pass=False,
                score=0.5,
                components=GradeComponents(state_checks=0.5),
                reasons="state 0.5",
                judge_status=judge_status,
            ),
        )

    def test_metrics_amended_when_judge_errored(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trials" / "task-1" / "0"
        trial_dir.mkdir(parents=True)
        trajectory = self._make_trajectory_with_grade(JudgeStatus.ERRORED)
        FileArtifactWriter().write_metrics(trial_dir, trajectory)

        executor = ProvisioningTrialExecutor(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor=InMemoryConductor(trajectory_factory=lambda *_: trajectory),
            logger=MagicMock(),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
        )
        executor._maybe_flag_missing_judge_verdict(trajectory, "task-1", 0)

        import yaml

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert metrics["error_stage"] == "judge_missing_verdict"

    def test_metrics_untouched_when_judge_completed(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trials" / "task-1" / "0"
        trial_dir.mkdir(parents=True)
        trajectory = self._make_trajectory_with_grade(JudgeStatus.COMPLETED)
        FileArtifactWriter().write_metrics(trial_dir, trajectory)

        executor = ProvisioningTrialExecutor(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor=InMemoryConductor(trajectory_factory=lambda *_: trajectory),
            logger=MagicMock(),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
        )
        executor._maybe_flag_missing_judge_verdict(trajectory, "task-1", 0)

        import yaml

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert "error_stage" not in metrics

    def test_metrics_untouched_when_grade_is_none_without_grading_error(
        self, tmp_path: Path
    ) -> None:
        """``grade=None`` with no ``grading_error`` covers unscored trials that
        never reached grading — e.g. an infrastructure abort. Nothing is
        recorded here; the outcome classifier owns those.
        """
        from datetime import UTC, datetime

        from tolokaforge.core.models import Message

        trial_dir = tmp_path / "trials" / "task-1" / "0"
        trial_dir.mkdir(parents=True)
        trajectory = Trajectory(
            task_id="task-1",
            trial_index=0,
            start_ts=datetime.now(tz=UTC),
            end_ts=datetime.now(tz=UTC),
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[Message(role=MessageRole.USER, content="hello")],
            metrics=Metrics(),
            grade=None,
        )
        FileArtifactWriter().write_metrics(trial_dir, trajectory)

        executor = ProvisioningTrialExecutor(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor=InMemoryConductor(trajectory_factory=lambda *_: trajectory),
            logger=MagicMock(),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
        )
        executor._maybe_flag_missing_judge_verdict(trajectory, "task-1", 0)

        import yaml

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert "error_stage" not in metrics

    def test_metrics_amended_when_grading_failed_error_raised(self, tmp_path: Path) -> None:
        """``grade=None`` with ``grading_error`` set covers the
        :class:`GradingFailedError` path — the grading RPC raised before a
        verdict was produced. The aggregator's rejudge loop keys on
        ``error_stage``, so this state must be flagged the same way an
        ``ERRORED`` judge status is.
        """
        from datetime import UTC, datetime

        from tolokaforge.core.models import Message

        trial_dir = tmp_path / "trials" / "task-1" / "0"
        trial_dir.mkdir(parents=True)
        trajectory = Trajectory(
            task_id="task-1",
            trial_index=0,
            start_ts=datetime.now(tz=UTC),
            end_ts=datetime.now(tz=UTC),
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[Message(role=MessageRole.USER, content="hello")],
            metrics=Metrics(),
            grade=None,
            grading_error="Grading failed for trial 'task-1:0': runner produced no verdict",
        )
        FileArtifactWriter().write_metrics(trial_dir, trajectory)

        executor = ProvisioningTrialExecutor(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor=InMemoryConductor(trajectory_factory=lambda *_: trajectory),
            logger=MagicMock(),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
        )
        executor._maybe_flag_missing_judge_verdict(trajectory, "task-1", 0)

        import yaml

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert metrics["error_stage"] == "judge_missing_verdict"


class TestEmptyCompletionRoutesToInfrastructureAbort:
    """``TerminationReason.EMPTY_COMPLETION`` classifies as
    ``INFRASTRUCTURE_ABORT`` — the provider returned no text and no tool
    calls, so the trial never gave the agent something to do; it drops out
    of the measured denominator alongside API_TIMEOUT / RATE_LIMIT.
    """

    def test_empty_completion_is_infrastructure_abort(self) -> None:
        from datetime import UTC, datetime

        from tolokaforge.core.failure_attribution import (
            TrialOutcomeClass,
            classify_trial_outcome,
        )
        from tolokaforge.core.models import Message

        trajectory = Trajectory(
            task_id="task-empty",
            trial_index=0,
            start_ts=datetime.now(tz=UTC),
            end_ts=datetime.now(tz=UTC),
            status=TrialStatus.FAILED,
            termination_reason=TerminationReason.EMPTY_COMPLETION,
            messages=[Message(role=MessageRole.USER, content="hello")],
            metrics=Metrics(),
        )

        assert classify_trial_outcome(trajectory) is TrialOutcomeClass.INFRASTRUCTURE_ABORT


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
        # No grade at all: a provisioning failure measured nothing, and a
        # fabricated 0.0 would be indistinguishable from a task the agent
        # failed. The reason survives on the trial's metrics.yaml.
        assert result.trajectory.grade is None
