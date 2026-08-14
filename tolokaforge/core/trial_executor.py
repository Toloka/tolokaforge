"""``TrialExecutor`` Protocol — the per-trial substrate-lifecycle seam.

The Cloud Runtime target (`docs/CLOUD_RUNTIME_ARCHITECTURE.md` §5.3) places
substrate ``provision`` / ``teardown`` on the scheduler-side lifeline that
brackets the Conductor's trial-body execution. This module defines the
swappable seam that owns that bracket:

* :class:`TrialExecutor` — Protocol with a single method ``execute`` that
  maps a :class:`TrialSpec` + :class:`TaskConfig` to a :class:`TrialResult`.
  Any orchestrator holds a ``TrialExecutor`` and delegates trial dispatch
  to it.
* :class:`ProvisioningTrialExecutor` — production implementation. Composes
  a :class:`RuntimeBackend` with a :class:`Conductor` and brackets each
  ``conductor.run`` call with substrate ``provision`` / ``await_ready`` /
  ``endpoints`` / ``teardown``. Post-provisioning, the per-trial URLs
  resolved by ``runtime_backend.endpoints(handle)`` are substituted into
  the ``TrialSpec.env_endpoints`` field the conductor sees.

The Protocol is deliberately narrow so a future ``RemoteTrialExecutor``
(gRPC client to a trial-plane worker) can replace the local composition
without touching the orchestrator.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml

from tolokaforge.core.models import (
    Metrics,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output_writer import METRICS_FILENAME
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    RunDisplayEvents,
)
from tolokaforge.core.runtime import EnvHandle, ProvisionError, RuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.conductor import Conductor
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.output.artifacts import TrialArtifactWriter
    from tolokaforge.core.trial import EnvEndpoints

__all__ = [
    "ProvisioningTrialExecutor",
    "TrialExecutor",
]


@runtime_checkable
class TrialExecutor(Protocol):
    """Per-trial dispatch seam — reads a :class:`TrialSpec` +
    :class:`TaskConfig`, returns a :class:`TrialResult`.

    Any orchestrator holds a ``TrialExecutor`` and submits it to its
    worker pool in place of ``conductor.run``. Implementations own
    whatever substrate lifecycle bracketing they need — local Docker
    Compose provisioning, remote gRPC dispatch, or nothing at all — and
    hide the mechanics behind a value-in / value-out contract.
    """

    def execute(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        """Execute one trial end-to-end and return its result."""
        ...


class ProvisioningTrialExecutor:
    """Production :class:`TrialExecutor`. Brackets a :class:`Conductor`'s
    ``run`` call with the injected :class:`RuntimeBackend`'s per-trial
    provisioning contract (ADR-0010).

    Composes three collaborators — ``runtime_backend`` for
    ``provision`` / ``await_ready`` / ``endpoints`` / ``teardown``,
    ``conductor`` for the trial body, and ``logger`` for per-branch
    structured observability at the substrate seam.

    On a diagnostics-worthy trial — one that fails execution or runs to
    completion but grades red — it captures per-service logs via
    ``runtime_backend.capture_service_logs`` before teardown, then records the
    captured byte counts on the trial's ``metrics.yaml`` (path derived from
    ``output_dir``; the amendment is a no-op when that file is absent).

    When provisioning itself fails, the trial body never runs and the
    conductor never writes the per-trial bundle. The executor writes a
    minimal one itself (``trajectory.yaml`` / ``metrics.yaml`` /
    ``grade.yaml``) via ``artifact_writer`` so post-hoc analysis and cost
    aggregation see a consistent trial-directory shape whether the trial
    completed or failed to provision.

    Instantiated once per run by the orchestrator's
    ``_build_trial_executor`` helper and submitted to the worker pool
    per trial. Provisioning parallelism = worker count.
    """

    def __init__(
        self,
        runtime_backend: RuntimeBackend,
        conductor: Conductor,
        logger: StructuredLogger,
        output_dir: Path,
        artifact_writer: TrialArtifactWriter,
        events: RunDisplayEvents = _NULL_EVENTS,
    ) -> None:
        self.runtime_backend = runtime_backend
        self.conductor = conductor
        self.logger = logger
        self.output_dir = output_dir
        self.artifact_writer = artifact_writer
        self.events = events

    def execute(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        self.logger.info(
            "Provisioning trial env",
            task_id=task_id,
            trial_index=trial_idx,
        )
        provision_start = time.monotonic()
        try:
            handle = self.runtime_backend.provision(spec)
        except ProvisionError as e:
            self.logger.error(
                "Provisioning failed",
                task_id=task_id,
                trial_index=trial_idx,
                stage=e.stage,
                error=e.reason,
            )
            result = _synthesize_provision_failure_result(spec, e)
            self._write_provision_failure_bundle(result.trajectory, e)
            return result

        try:
            self.runtime_backend.await_ready(handle)
        except ProvisionError as e:
            self.logger.error(
                "Provisioning failed",
                task_id=task_id,
                trial_index=trial_idx,
                stage=e.stage,
                error=e.reason,
            )
            self._safe_teardown(handle, task_id, trial_idx)
            result = _synthesize_provision_failure_result(spec, e)
            self._write_provision_failure_bundle(result.trajectory, e)
            return result

        try:
            real_endpoints = self.runtime_backend.endpoints(handle)
            provisioning_duration_s = time.monotonic() - provision_start
            containers = self.runtime_backend.get_infrastructure_snapshot(handle)
            self.events.trial_provisioned(
                trial_id=spec.trial_id,
                containers=containers,
                endpoints=_endpoints_to_map(real_endpoints),
            )
            final_spec = spec.model_copy(update={"env_endpoints": real_endpoints})
            self.logger.info(
                "Trial env provisioned",
                task_id=task_id,
                trial_index=trial_idx,
                runner_url=real_endpoints.runner_url,
            )
            result = self.conductor.run(final_spec, task_config)
            self._amend_trial_metrics(
                task_id,
                trial_idx,
                {"provisioning_duration_s": round(provisioning_duration_s, 3)},
            )
            self._capture_service_logs(handle, result, task_id, trial_idx)
            return result
        finally:
            self._safe_teardown(handle, task_id, trial_idx)

    def _capture_service_logs(
        self, handle: EnvHandle, result: TrialResult, task_id: str, trial_idx: int
    ) -> None:
        """Capture per-service logs for a diagnostics-worthy trial, before teardown.

        A trial is capture-worthy when it either fails execution
        (``ERROR`` / ``TIMEOUT``) or runs to completion but grades red
        (``COMPLETED`` with ``grade.binary_pass is False``) — the case where a
        task author needs service output to diagnose why the agent's mutations
        did not land. A completed trial that passes, or one with no grade, is
        not capture-worthy. The backend gates the actual write on this verdict
        plus its on-success policy. When the backend returns a non-empty byte
        map, emit the aggregate summary line and amend the trial's
        ``metrics.yaml`` with ``captured_service_logs``. Best-effort
        diagnostics captured *because* the outcome is already decided: never
        changes control flow.
        """
        trajectory = result.trajectory
        capture_worthy = trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT) or (
            trajectory.grade is not None and trajectory.grade.binary_pass is False
        )
        byte_map = self.runtime_backend.capture_service_logs(handle, capture_worthy=capture_worthy)
        if not byte_map:
            return
        self.logger.info(
            "trial.service_logs_captured",
            task_id=task_id,
            trial_index=trial_idx,
            services=byte_map,
        )
        self._amend_trial_metrics(task_id, trial_idx, {"captured_service_logs": dict(byte_map)})

    def _write_provision_failure_bundle(
        self, trajectory: Trajectory, error: ProvisionError
    ) -> None:
        """Persist a minimal trial bundle for a trial whose environment never
        came up: ``trajectory.yaml`` / ``metrics.yaml`` / ``grade.yaml`` under
        ``output_dir/trials/{task_id}/{trial_index}/``.

        The conductor never ran, so nothing else writes this trial's directory.
        Reuses the run's ``artifact_writer`` (no schema duplication), then amends
        ``metrics.yaml`` with the top-level failure signal (``error`` /
        ``error_reason``) via the shared ``_amend_trial_metrics`` path. Any write
        failure is logged and swallowed — mirrors ``_safe_teardown``; it never
        masks the synthesized ``ProvisionError`` result the caller returns.
        """
        task_id = trajectory.task_id
        trial_idx = trajectory.trial_index
        trial_dir = self.output_dir / "trials" / task_id / str(trial_idx)
        try:
            self.artifact_writer.write_trajectory(trial_dir, trajectory)
            self.artifact_writer.write_metrics(trial_dir, trajectory)
            if trajectory.grade is not None:
                self.artifact_writer.write_grade(trial_dir, trajectory.grade)
        except Exception as write_err:  # noqa: BLE001 — best-effort diagnostic bundle
            self.logger.warning(
                "Writing provision-failure bundle failed; continuing",
                task_id=task_id,
                trial_index=trial_idx,
                error=str(write_err),
            )
            return
        self._amend_trial_metrics(
            task_id,
            trial_idx,
            {
                "error": TerminationReason.PROVISION_ERROR.value,
                "error_reason": error.reason,
            },
        )

    def _amend_trial_metrics(self, task_id: str, trial_idx: int, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into the trial's ``metrics.yaml`` as top-level keys.

        Read-add-write of the plain YAML mapping the conductor already wrote —
        the durable landing spot for host-side per-trial values
        (``provisioning_duration_s``, ``captured_service_logs``). No-op when the
        file is absent. Logs and continues on I/O failure so a diagnostic write
        never masks the trial result.
        """
        metrics_path = self.output_dir / "trials" / task_id / str(trial_idx) / METRICS_FILENAME
        if not metrics_path.exists():
            return
        try:
            with metrics_path.open() as f:
                metrics = yaml.safe_load(f) or {}
            metrics.update(updates)
            with metrics_path.open("w") as f:
                yaml.safe_dump(metrics, f, sort_keys=False)
        except Exception as amend_err:  # noqa: BLE001 — best-effort diagnostic amendment
            self.logger.warning(
                "Amending metrics.yaml failed; continuing",
                task_id=task_id,
                trial_index=trial_idx,
                error=str(amend_err),
            )

    def _safe_teardown(self, handle: EnvHandle, task_id: str, trial_idx: int) -> None:
        """Best-effort teardown that logs failures instead of swallowing them.

        Substrate teardown after a failed trial body must not mask the
        original error the caller cares about, but a silent ``except:
        pass`` leaves orphaned resources undiagnosable. Log-and-continue
        gives operators visibility without changing control flow.
        """
        try:
            self.runtime_backend.teardown(handle)
        except Exception as teardown_err:  # noqa: BLE001 — best-effort by contract
            self.logger.warning(
                "Teardown raised; continuing",
                task_id=task_id,
                trial_index=trial_idx,
                error=str(teardown_err),
            )
            return
        self.logger.info(
            "Trial env teardown complete",
            task_id=task_id,
            trial_index=trial_idx,
        )


def _split_trial_id(trial_id: str) -> tuple[str, int]:
    """Return ``(task_id, trial_index)`` from a canonical ``"{task_id}:{idx}"`` id."""
    task_id, idx_s = trial_id.rsplit(":", 1)
    return task_id, int(idx_s)


def _endpoints_to_map(endpoints: EnvEndpoints) -> dict[str, str]:
    """Reshape :class:`EnvEndpoints` into the ``{role → url}`` map the
    display carries in ``trial_provisioned``. ``None`` values are skipped
    so the panel doesn't render half-populated rows."""
    mapping: dict[str, str] = {}
    if endpoints.runner_url:
        mapping["runner"] = endpoints.runner_url
    if endpoints.db_url:
        mapping["db"] = endpoints.db_url
    if endpoints.rag_url:
        mapping["rag"] = endpoints.rag_url
    return mapping


def _synthesize_provision_failure_result(spec: TrialSpec, error: ProvisionError) -> TrialResult:
    """Build a failed :class:`TrialResult` for a trial whose environment
    never came up.

    Materialises a :class:`Trajectory` with
    :attr:`TerminationReason.PROVISION_ERROR` and **no grade**: the trial body
    never ran, so there is no performance to score, and a ``0.0`` would count as
    a task the model failed. The exception's stage and reason reach the durable
    record through ``metrics.yaml`` (see :meth:`_write_provision_failure_bundle`),
    which is what failure attribution and dashboards read to tell substrate
    failures from tool / grader / model-reasoning failures. Provisioning failures
    are deterministic, so
    :meth:`~tolokaforge.core.orchestrator.Orchestrator._is_retryable_trajectory`
    classifies ``PROVISION_ERROR`` as non-retryable and fails fast rather
    than burning a fresh ``provision()`` on each attempt.
    """
    task_id, trial_idx = _split_trial_id(spec.trial_id)
    now = datetime.now(UTC)
    trajectory = Trajectory(
        task_id=task_id,
        trial_index=trial_idx,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.ERROR,
        termination_reason=TerminationReason.PROVISION_ERROR,
        messages=[],
        metrics=Metrics(),
    )
    return TrialResult.from_trajectory(
        trial_id=spec.trial_id, trajectory=trajectory, worker_id=spec.worker_id
    )
