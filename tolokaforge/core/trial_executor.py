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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.runtime import EnvHandle, ProvisionError, RuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec

if TYPE_CHECKING:
    from tolokaforge.core.conductor import Conductor
    from tolokaforge.core.logging import StructuredLogger

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

    Instantiated once per run by the orchestrator's
    ``_build_trial_executor`` helper and submitted to the worker pool
    per trial. Provisioning parallelism = worker count.
    """

    def __init__(
        self,
        runtime_backend: RuntimeBackend,
        conductor: Conductor,
        logger: StructuredLogger,
    ) -> None:
        self.runtime_backend = runtime_backend
        self.conductor = conductor
        self.logger = logger

    def execute(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        self.logger.info(
            "Provisioning trial env",
            task_id=task_id,
            trial_index=trial_idx,
        )
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
            return _synthesize_provision_failure_result(spec, e)

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
            return _synthesize_provision_failure_result(spec, e)

        try:
            real_endpoints = self.runtime_backend.endpoints(handle)
            final_spec = spec.model_copy(update={"env_endpoints": real_endpoints})
            self.logger.info(
                "Trial env provisioned",
                task_id=task_id,
                trial_index=trial_idx,
                runner_url=real_endpoints.runner_url,
            )
            return self.conductor.run(final_spec, task_config)
        finally:
            self._safe_teardown(handle, task_id, trial_idx)

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


def _synthesize_provision_failure_result(spec: TrialSpec, error: ProvisionError) -> TrialResult:
    """Build a failed :class:`TrialResult` for a trial whose environment
    never came up.

    Materialises a :class:`Trajectory` with
    :attr:`TerminationReason.PROVISION_ERROR` and a fail-:class:`Grade`
    that carries the exception's ``reason`` so downstream analytics
    (failure attribution, dashboards) can distinguish substrate failures
    from tool / grader / model-reasoning failures. Provisioning failures
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
        grade=Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(state_checks=0.0),
            reasons=f"Provisioning failed at {error.stage}: {error.reason}",
        ),
    )
    return TrialResult.from_trajectory(
        trial_id=spec.trial_id, trajectory=trajectory, worker_id=spec.worker_id
    )
