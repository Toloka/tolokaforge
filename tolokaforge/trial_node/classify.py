"""Classify a trial-run exception into the trial-node ``error_type`` vocabulary.

One trial runs the same way whether it runs on a real node (over the
``RunTrial`` RPC) or in a local thread: both drive
:func:`~tolokaforge.core.trial_node.run_trial_from_inputs` and must agree on
what each failure means. :func:`classify_trial_exception` is the single place
that maps a raised exception to a :class:`~tolokaforge.trial_node.protocol.NodeErrorType`
value plus its ``stage`` and operator-facing ``reason``, so the node service and
the ``local`` dispatch adapter's thread mode read failures identically.
"""

from __future__ import annotations

from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial_node import TaskPackDriftError
from tolokaforge.trial_node.protocol import NodeErrorType

__all__ = ["classify_trial_exception"]


def classify_trial_exception(exc: BaseException) -> tuple[str, str, str]:
    """Return the ``(error_type, stage, reason)`` a trial failure maps to.

    A substrate failure is a :class:`~tolokaforge.core.runtime.ProvisionError`
    carrying its own stage and reason; a drifted task pack, a non-node-runnable
    adapter and an unknown implementation name are deterministic refusals;
    anything else — a stray :class:`ValueError` deep in the trial included — is
    an internal fault a retry may clear.
    """
    if isinstance(exc, ProvisionError):
        return (NodeErrorType.PROVISION_ERROR, exc.stage, exc.reason)
    if isinstance(exc, TaskPackDriftError):
        return (NodeErrorType.TASK_PACK_DRIFT, "task_pack", str(exc))
    if isinstance(exc, NotImplementedError):
        return (NodeErrorType.NOT_IMPLEMENTED, "adapter", str(exc))
    if isinstance(exc, UnknownImplementationError):
        return (NodeErrorType.INVALID_CONFIG, "inputs", str(exc))
    return (NodeErrorType.INTERNAL, "run", f"{type(exc).__name__}: {exc}")
