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

from pathlib import Path

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_trial_spec
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
from tolokaforge.core.output_writer import TRIAL_BUNDLE_SCHEMA_VERSION
from tolokaforge.core.runtime import InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial import TrialResult
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

pytestmark = pytest.mark.canonical


class _FailProvisionBackend(InMemoryRuntimeBackend):
    """Backend whose ``provision`` always raises with a caller-chosen reason."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    def provision(self, spec):  # type: ignore[override]
        raise ProvisionError(trial_id=spec.trial_id, stage="provision", reason=self._reason)


def _make_executor(
    backend: InMemoryRuntimeBackend,
    output_dir: Path,
    artifact_writer,
) -> ProvisioningTrialExecutor:
    return ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=InMemoryConductor(),
        logger=StructuredLogger("test-provision-failure-bundle"),
        output_dir=output_dir,
        artifact_writer=artifact_writer,
    )


def _trial_dir(output_dir: Path, task_id: str, trial_idx: int) -> Path:
    return output_dir / "trials" / task_id / str(trial_idx)


class TestBundleWrittenOnProvisionFailure:
    def test_bundle_lands_on_disk_with_failure_signal(self, tmp_path: Path) -> None:
        backend = _FailProvisionBackend("no capacity in region")
        executor = _make_executor(backend, tmp_path, FileArtifactWriter())

        result = executor.execute(
            make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1")
        )

        assert isinstance(result, TrialResult)
        assert result.trajectory.status is TrialStatus.ERROR

        trial_dir = _trial_dir(tmp_path, "task-1", 0)
        assert (trial_dir / "trajectory.yaml").exists()
        assert (trial_dir / "metrics.yaml").exists()
        # The heavy conductor snapshot never runs on this path.
        assert not (trial_dir / "task.yaml").exists()
        assert not (trial_dir / "env.yaml").exists()
        assert not (trial_dir / "logs.yaml").exists()

        trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
        assert trajectory["status"] == "error"
        assert trajectory["termination_reason"] == "provision_error"

        metrics = yaml.safe_load((trial_dir / "metrics.yaml").read_text())
        # Default-``Metrics`` shape: ``cost_usd`` is ``None`` until an API call
        # prices the trial; ``_collect_existing_cost`` reads it as zero.
        assert metrics["cost_usd"] is None
        assert metrics["schema_version"] == TRIAL_BUNDLE_SCHEMA_VERSION
        assert metrics["error"] == "provision_error"
        assert metrics["error_reason"] == "no capacity in region"

        # No ``grade.yaml``: the trial body never ran, so there is no verdict.
        # The failure is recorded where it happened — the trajectory's
        # termination reason and the metrics error fields above.
        assert not (trial_dir / "grade.yaml").exists()

    def test_collect_existing_cost_reads_the_failed_trial(self, tmp_path: Path) -> None:
        backend = _FailProvisionBackend("boom")
        executor = _make_executor(backend, tmp_path, FileArtifactWriter())

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0


class TestErrorReasonPropagation:
    def test_metrics_error_reason_carries_provision_error_reason(self, tmp_path: Path) -> None:
        backend = _FailProvisionBackend("compose pull failed: image not found")
        executor = _make_executor(backend, tmp_path, FileArtifactWriter())

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
        backend = _FailProvisionBackend("boom")
        logger = StructuredLogger("test-provision-failure-bundle")
        executor = ProvisioningTrialExecutor(
            runtime_backend=backend,
            conductor=InMemoryConductor(),
            logger=logger,
            output_dir=tmp_path,
            artifact_writer=_RaisingWriter(),
        )

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
