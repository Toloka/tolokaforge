"""Trial-node wire-protocol version, structured outcomes and the errors both ends raise.

The control plane declares :data:`NODE_PROTOCOL_VERSION` on every ``RunTrial``
request; the node refuses a caller that declares less than its own value before
doing any work, and the control plane refuses a node that reports less than its
own value on every reply. Placing the gate in front of the trial means a skewed
pair fails loudly with nothing spent, instead of running a trial the other end
cannot read back.

The version is independent of the engine/runner protocol version. What each
version is the first to change is the ledger in ``docs/GRPC_PROTOCOL.md``,
which a canonical test holds level with this constant.

:class:`NodeTrialOutcome` is the structured reply of one ``RunTrial`` call.
:class:`NodeRefusedError` and :class:`NodeTransientError` split a failed call
into the two shapes the control plane treats differently: a deterministic
refusal that retrying cannot change, and an infrastructure fault a retry may
clear. This module imports nothing from gRPC so the control plane's dispatch
code and the node service can share it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "NODE_PROTOCOL_VERSION",
    "NodeErrorType",
    "NodeRefusedError",
    "NodeTransientError",
    "NodeTrialOutcome",
    "check_node_version",
]

NODE_PROTOCOL_VERSION = 2


class NodeErrorType:
    """The ``error_type`` vocabulary of a failed ``RunTrial`` reply."""

    PROVISION_ERROR = "ProvisionError"
    SECRET_PREFLIGHT = "SecretPreflight"
    TASK_PACK_DRIFT = "TaskPackDrift"
    NOT_IMPLEMENTED = "NotImplemented"
    VERSION_SKEW = "VersionSkew"
    INVALID_CONFIG = "InvalidConfig"
    NODE_BUSY = "NodeBusy"
    INTERNAL = "Internal"
    MESSAGE_SIZE_SKEW = "MessageSizeSkew"
    """Raised by the client's pre-flight; a node never sends it."""


@dataclass(frozen=True)
class NodeTrialOutcome:
    """The structured reply of one ``RunTrial`` call.

    ``trial_result_json`` is set exactly when ``success``; ``error_type`` /
    ``stage`` / ``reason`` describe the failure otherwise.
    """

    success: bool
    trial_result_json: str = ""
    error_type: str = ""
    stage: str = ""
    reason: str = ""
    node_protocol_version: int = 0


class NodeRefusedError(RuntimeError):
    """The node (or the client's pre-flight) refused the trial for a reason a retry cannot change.

    Fields:
        error_type: A :class:`NodeErrorType` value.
        stage: The step that refused (``preflight``, ``protocol``, ...), or empty.
        reason: What was wrong, naming what the operator must change.
    """

    def __init__(self, error_type: str, stage: str, reason: str) -> None:
        super().__init__(f"[{error_type}{f'/{stage}' if stage else ''}] {reason}")
        self.error_type = error_type
        self.stage = stage
        self.reason = reason


class NodeTransientError(RuntimeError):
    """The node could not serve the trial for an infrastructure reason a retry may clear.

    Covers a node that is down or unreachable, one already running a trial, and
    an unexpected failure inside the node.
    """


def check_node_version(node_protocol_version: int, *, node_address: str) -> None:
    """Raise :class:`NodeRefusedError` when the node's protocol is older than this client's.

    A node reporting ``0`` predates the version field and is refused too.
    """
    if node_protocol_version < NODE_PROTOCOL_VERSION:
        raise NodeRefusedError(
            NodeErrorType.VERSION_SKEW,
            "protocol",
            f"node {node_address} speaks trial-node protocol {node_protocol_version} but this "
            f"engine requires {NODE_PROTOCOL_VERSION}; upgrade the node image to match the engine.",
        )
