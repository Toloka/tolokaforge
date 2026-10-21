"""The dispatch-adapter seam: where a whole trial runs.

A :class:`~tolokaforge.core.dispatch.adapter.DispatchAdapter` takes one trial's
:class:`~tolokaforge.core.trial.TrialNodeInputs` and returns its graded
:class:`~tolokaforge.core.trial.TrialResult`. Adapters register under the
``tolokaforge.dispatch_adapters`` entry-point group and raise the
transport-neutral exceptions in :mod:`tolokaforge.core.dispatch.errors`.

The control-plane contract — :class:`ControllableDispatchAdapter` and its
``JobHandle`` / ``JobState`` / ``JobStatus`` value types — is the optional
sibling an adapter implements to be operated after a job is submitted (cancel,
status, result collection). The concrete adapter submodules (``local``,
``grpc_node``) are imported explicitly by their users.
"""

from tolokaforge.core.dispatch.adapter import ControllableDispatchAdapter
from tolokaforge.core.dispatch.control import JobHandle, JobState, JobStatus
from tolokaforge.core.dispatch.errors import DispatchUnsupported

__all__ = [
    "ControllableDispatchAdapter",
    "DispatchUnsupported",
    "JobHandle",
    "JobState",
    "JobStatus",
]
