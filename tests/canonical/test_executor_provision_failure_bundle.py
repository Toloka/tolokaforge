"""Lock the provision-failure trial-bundle contract on ``ProvisioningTrialExecutor``.

When ``runtime_backend.provision`` raises :class:`ProvisionError`, the trial
body never runs and the conductor never writes the per-trial directory. The
executor writes a minimal bundle itself via its ``artifact_writer``:
``trajectory.yaml`` (``status: error`` / ``termination_reason:
provision_error``), ``metrics.yaml`` (default-``Metrics`` shape plus top-level
``error: provision_error`` + ``error_reason``), and ``grade.yaml``
(``binary_pass: false`` / ``score: 0.0``). Bundle-write failure is best-effort
and never masks the synthesized failed :class:`TrialResult`.

No Docker: an :class:`InMemoryRuntimeBackend` subclass raises ``ProvisionError``
from ``provision``, a real :class:`FileArtifactWriter` writes to ``tmp_path``,
and an :class:`InMemoryConductor` stands in for the (never-invoked) trial body.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.utils.provision_failure import FailProvisionBackend, provisioning_executor
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Message,
    MessageRole,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
from tolokaforge.core.output_writer import TRIAL_BUNDLE_SCHEMA_VERSION
from tolokaforge.core.runtime import InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

pytestmark = pytest.mark.canonical


def _trial_dir(output_dir: Path, task_id: str, trial_idx: int) -> Path:
    return output_dir / "trials" / task_id / str(trial_idx)


class TestBundleWrittenOnProvisionFailure:
    def test_bundle_lands_on_disk_with_failure_signal(self, tmp_path: Path) -> None:
        backend = FailProvisionBackend("no capacity in region")
        executor = provisioning_executor(
            backend,
            tmp_path,
            FileArtifactWriter(),
            logger=StructuredLogger("test-provision-failure-bundle"),
        )

        result = executor.execute(
            make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1")
        )

        assert isinstance(result, TrialResult)
        assert result.trajectory.status is TrialStatus.ERROR

        trial_dir = _trial_dir(tmp_path, "task-1", 0)
        assert (trial_dir / "trajectory.yaml").exists()
        assert (trial_dir / "metrics.yaml").exists()
        # The heavy conductor snapshot never runs on this path. Nor does the trial,
        # so there is no tool-call record to write and the bundle carries none —
        # which the schema stamp does not and must not promise otherwise.
        assert not (trial_dir / "task.yaml").exists()
        assert not (trial_dir / "env.yaml").exists()
        assert not (trial_dir / "logs.yaml").exists()
        assert not (trial_dir / "tool_log.yaml").exists()

        trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
        assert trajectory["status"] == "error"
        assert trajectory["termination_reason"] == "provision_error"
        assert trajectory["provision_stage"] == "provision"

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        # Default-``Metrics`` shape: ``cost_usd`` is ``None`` until an API call
        # prices the trial; ``_collect_existing_cost`` reads it as zero.
        assert metrics["cost_usd"] is None
        assert metrics["schema_version"] == TRIAL_BUNDLE_SCHEMA_VERSION
        assert metrics["error"] == "provision_error"
        assert metrics["error_reason"] == "no capacity in region"
        assert metrics["error_stage"] == "provision"

        # No ``grade.yaml``: the trial body never ran, so there is no verdict.
        # The failure is recorded where it happened — the trajectory's
        # termination reason and the metrics error fields above.
        assert not (trial_dir / "grade.yaml").exists()

    def test_collect_existing_cost_reads_the_failed_trial(self, tmp_path: Path) -> None:
        backend = FailProvisionBackend("boom")
        executor = provisioning_executor(
            backend,
            tmp_path,
            FileArtifactWriter(),
            logger=StructuredLogger("test-provision-failure-bundle"),
        )

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0


class TestErrorReasonPropagation:
    def test_metrics_error_reason_carries_provision_error_reason(self, tmp_path: Path) -> None:
        backend = FailProvisionBackend("compose pull failed: image not found")
        executor = provisioning_executor(
            backend,
            tmp_path,
            FileArtifactWriter(),
            logger=StructuredLogger("test-provision-failure-bundle"),
        )

        executor.execute(make_trial_spec(trial_id="task-9:3"), make_task_config(task_id="task-9"))

        metrics = yaml.safe_load((_trial_dir(tmp_path, "task-9", 3) / "metrics.yaml").read_text())
        assert metrics["error_reason"] == "compose pull failed: image not found"


class _RaisingWriter(InMemoryArtifactWriter):
    """Artifact writer whose ``write_trajectory`` raises, to prove the
    executor swallows bundle-write failures without masking the result."""

    def write_trajectory(self, trial_dir: Path, trajectory: Trajectory) -> None:
        raise OSError("disk full")


class TestBundleWriteFailureDoesNotMask:
    def test_write_failure_is_logged_and_result_still_returned(self, tmp_path: Path) -> None:
        backend = FailProvisionBackend("boom")
        logger = StructuredLogger("test-provision-failure-bundle")
        executor = provisioning_executor(backend, tmp_path, _RaisingWriter(), logger=logger)

        result = executor.execute(
            make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1")
        )

        assert isinstance(result, TrialResult)
        assert result.trajectory.status is TrialStatus.ERROR
        assert result.trajectory.termination_reason is not None

        warnings = [
            e
            for e in logger.logs
            if e["message"] == "Writing provision-failure bundle failed; continuing"
        ]
        assert len(warnings) == 1
        assert warnings[0]["level"] == "WARNING"


class _RegisterTrialRefusingConductor(InMemoryConductor):
    """Conductor whose ``run`` raises ``ProvisionError(stage='register_trial')``.

    The register-trial refusal reaches the trial executor through
    ``conductor.run``: registration is a per-trial RPC arming step the runner
    performs inside the trial body, so a stage-``register_trial`` raise is
    always downstream of a successful ``provision`` + ``await_ready``. This
    stand-in reproduces that shape without a runner service.
    """

    def run(self, spec: TrialSpec, task_config):
        raise ProvisionError(
            trial_id=spec.trial_id,
            stage="register_trial",
            reason="runner refused registration: search plane unavailable",
        )


class TestStageSurvivesToMetricsAndTrajectory:
    def test_register_trial_stage_survives_from_conductor_raise(self, tmp_path: Path) -> None:
        """A ``ProvisionError`` raised inside ``conductor.run`` carries its stage
        onto both durable surfaces — the trajectory field and the metrics key —
        even though the failure lands after ``provision`` + ``await_ready``
        succeeded."""
        executor = ProvisioningTrialExecutor(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor=_RegisterTrialRefusingConductor(),
            logger=StructuredLogger("test-provision-failure-bundle"),
            output_dir=tmp_path,
            artifact_writer=FileArtifactWriter(),
        )

        result = executor.execute(
            make_trial_spec(trial_id="task-7:0"), make_task_config(task_id="task-7")
        )

        assert isinstance(result, TrialResult)
        assert result.trajectory.status is TrialStatus.ERROR
        assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR
        assert result.trajectory.provision_stage == "register_trial"

        trial_dir = _trial_dir(tmp_path, "task-7", 0)
        trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
        assert trajectory["provision_stage"] == "register_trial"

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert metrics["error"] == "provision_error"
        assert metrics["error_stage"] == "register_trial"
        assert metrics["error_reason"] == "runner refused registration: search plane unavailable"


class TestErrorStageIsAbsentOffTheProvisionFailurePath:
    def test_completed_trial_metrics_yaml_carries_no_error_stage(self, tmp_path: Path) -> None:
        """The ``error_stage`` key is written only when
        :meth:`_write_provision_failure_bundle` runs. A ``metrics.yaml`` from a
        completed trial — the artifact writer's own output, unamended — has no
        ``error_stage`` key at all, so the invariant "error_stage iff
        error == provision_error" reads one direction from the file."""
        trial_dir = _trial_dir(tmp_path, "task-completed", 0)
        trial_dir.mkdir(parents=True)
        trajectory = Trajectory(
            task_id="task-completed",
            trial_index=0,
            start_ts=datetime.now(tz=timezone.utc),
            end_ts=datetime.now(tz=timezone.utc),
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[Message(role=MessageRole.USER, content="hello")],
            metrics=Metrics(),
        )
        FileArtifactWriter().write_metrics(trial_dir, trajectory)

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        assert "error" not in metrics
        assert "error_reason" not in metrics
        assert "error_stage" not in metrics
