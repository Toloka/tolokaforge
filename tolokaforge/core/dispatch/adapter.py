"""``DispatchAdapter`` Protocol: hand one whole trial to a place that runs it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.dispatch.control import JobHandle, JobStatus
    from tolokaforge.core.trial import TrialNodeInputs, TrialResult

__all__ = ["ControllableDispatchAdapter", "DispatchAdapter"]


@runtime_checkable
class DispatchAdapter(Protocol):
    """Runs a whole trial somewhere other than the orchestrator's own substrate.

    Where an ordinary runtime backend provisions an environment the caller's
    conductor then drives, a dispatch adapter ships the trial's inputs elsewhere
    and returns the graded result. Only an object satisfying this Protocol is
    dispatched through :class:`~tolokaforge.core.trial_executor.RemoteTrialExecutor`.
    """

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        """Run one trial to a graded result on the adapter's own substrate.

        Raises:
            DispatchProvisionError: the substrate failed to come up.
            DispatchRefused: the substrate refused the trial for a reason a
                retry cannot change.
            DispatchTransient: the substrate could not serve the trial; a retry
                may succeed.

        Implementations raise these transport-neutral errors from
        :mod:`tolokaforge.core.dispatch.errors`; the built-in ``grpc_node``
        adapter completes that wiring in a later slice.
        """
        ...

    def connect(self) -> None:
        """Establish the adapter's run-level connection, once before any trial.

        The contact budget comes from ``DispatchAdapterBuildContext``.
        """
        ...

    def close(self) -> None:
        """Release run-level resources; called once after the last trial."""
        ...


@runtime_checkable
class ControllableDispatchAdapter(DispatchAdapter, Protocol):
    """A :class:`DispatchAdapter` that also exposes the control-plane operations.

    ``run_trial_node`` is the synchronous submit-and-await convenience every
    adapter has; this sibling Protocol adds the id-keyed operations that let the
    orchestrator operate a job after it is submitted: cancel it, read its state,
    and collect its result. It is a separate Protocol, not extra methods on
    :class:`DispatchAdapter`, so a third-party three-method adapter keeps
    satisfying ``DispatchAdapter`` unchanged.

    An adapter whose backend cannot honour one of these raises
    :class:`~tolokaforge.core.dispatch.errors.DispatchUnsupported`.
    """

    def cancel(self, handle: JobHandle) -> None:
        """Request cancellation of the job the handle names. Idempotent."""
        ...

    def status(self, handle: JobHandle) -> JobStatus:
        """Return the job's current state; an unknown id is ``JobState.UNKNOWN``."""
        ...

    def fetch_result(self, handle: JobHandle) -> TrialResult:
        """Return the job's graded result, or raise ``DispatchUnsupported``."""
        ...
