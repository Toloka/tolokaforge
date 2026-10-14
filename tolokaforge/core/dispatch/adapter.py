"""``DispatchAdapter`` Protocol: hand one whole trial to a place that runs it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.trial import TrialNodeInputs, TrialResult

__all__ = ["DispatchAdapter"]


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
            ProvisionError: the substrate failed to come up.
            NodeRefusedError: the substrate refused the trial for a reason a
                retry cannot change.
            NodeTransientError: the substrate could not serve the trial; a retry
                may succeed.
        """
        ...
