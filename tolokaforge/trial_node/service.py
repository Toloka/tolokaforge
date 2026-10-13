"""Trial node service — the whole-trial ``RunTrial`` RPC.

Answers :class:`trial_node_pb2.RunTrialRequest` by running one trial to a graded
:class:`~tolokaforge.core.trial.TrialResult` through
:func:`~tolokaforge.core.trial_node.run_trial_from_inputs`: the node's own
conductor, agent loop, grader and runtime backend, with the trial's substrate
brought up on the node. The reply is always a structured
:class:`trial_node_pb2.RunTrialResponse`; a failure is an ``error_type`` plus
``stage`` and ``reason``, never a transport error, so the control plane can tell
a deterministic refusal from an infrastructure fault.

One trial runs at a time: the process secret manager is global, so a second
concurrent ``RunTrial`` is answered ``NodeBusy`` rather than allowed to corrupt
the running trial's credentials. Before any substrate is provisioned the node
resolves every ``${secret:NAME}`` reference in the inputs against its own secret
payload and refuses with ``SecretPreflight`` naming the missing ones.

The node supplies its own filesystem placement. Trial artifacts go under
``output_root/<run_id>``, written by the node's conductor before it replies; and
when a ``task_pack_root`` is configured the task's files are read from
``task_pack_root/<task_id>`` — the content hash the inputs carry is still
verified against whatever is mounted there.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc

from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_node import (
    TaskPackDriftError,
    install_container_secrets,
    run_trial_from_inputs,
)
from tolokaforge.secrets import UnresolvedReferenceError, expand_secret_refs, get_default
from tolokaforge.trial_node import trial_node_pb2, trial_node_pb2_grpc
from tolokaforge.trial_node.protocol import NODE_PROTOCOL_VERSION, NodeErrorType

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger

RunTrialFn = Callable[[TrialNodeInputs], TrialResult]
"""Runs one trial from inputs; the service defaults it to ``run_trial_from_inputs``."""


class TrialNodeServiceImpl(trial_node_pb2_grpc.TrialNodeServiceServicer):
    """gRPC servicer running one whole trial per ``RunTrial`` call."""

    def __init__(
        self,
        *,
        logger: StructuredLogger,
        max_message_mb: int,
        output_root: Path,
        task_pack_root: Path | None = None,
        run_trial: RunTrialFn = run_trial_from_inputs,
    ) -> None:
        self._logger = logger
        self._max_message_mb = max_message_mb
        self._output_root = output_root
        self._task_pack_root = task_pack_root
        self._run_trial = run_trial
        self._trial_lock = threading.Lock()

    def RunTrial(  # noqa: N802 — gRPC method name
        self, request: trial_node_pb2.RunTrialRequest, context: grpc.ServicerContext
    ) -> trial_node_pb2.RunTrialResponse:
        del context
        if request.client_protocol_version < NODE_PROTOCOL_VERSION:
            return self._failure(
                NodeErrorType.VERSION_SKEW,
                "protocol",
                f"caller speaks trial-node protocol {request.client_protocol_version} but "
                f"this node requires {NODE_PROTOCOL_VERSION}; upgrade the engine.",
            )
        try:
            inputs = TrialNodeInputs.model_validate_json(request.trial_node_inputs_json)
        except ValueError as exc:
            return self._failure(
                NodeErrorType.INVALID_CONFIG,
                "inputs",
                f"trial inputs were rejected by this node's schema: {exc}",
            )
        if not self._trial_lock.acquire(blocking=False):
            return self._failure(
                NodeErrorType.NODE_BUSY, "", "this node is already running a trial"
            )
        try:
            return self._run(inputs)
        finally:
            self._trial_lock.release()

    def HealthCheck(  # noqa: N802 — gRPC method name
        self, request: trial_node_pb2.HealthCheckRequest, context: grpc.ServicerContext
    ) -> trial_node_pb2.HealthCheckResponse:
        del request, context
        in_flight = self._trial_lock.locked()
        return trial_node_pb2.HealthCheckResponse(
            status="busy" if in_flight else "ready",
            node_protocol_version=NODE_PROTOCOL_VERSION,
            node_max_message_mb=self._max_message_mb,
            trial_in_flight=in_flight,
        )

    def _run(self, inputs: TrialNodeInputs) -> trial_node_pb2.RunTrialResponse:
        trial_id = inputs.trial_spec.trial_id
        try:
            install_container_secrets()
            problems = _unresolvable_secret_references(inputs)
        except ValueError as exc:
            return self._failure(NodeErrorType.SECRET_PREFLIGHT, "preflight", str(exc))
        if problems:
            return self._failure(
                NodeErrorType.SECRET_PREFLIGHT,
                "preflight",
                "the trial inputs carry secret references this node cannot resolve: "
                f"{'; '.join(problems)}. Set the missing names in the node's "
                "TOLOKAFORGE_SECRETS_JSON.",
            )
        self._logger.info("Running trial", trial_id=trial_id)
        try:
            result = self._run_trial(self._place(inputs))
        except ProvisionError as exc:
            return self._failure(NodeErrorType.PROVISION_ERROR, exc.stage, exc.reason)
        except TaskPackDriftError as exc:
            return self._failure(NodeErrorType.TASK_PACK_DRIFT, "task_pack", str(exc))
        except NotImplementedError as exc:
            return self._failure(NodeErrorType.NOT_IMPLEMENTED, "adapter", str(exc))
        except UnknownImplementationError as exc:
            return self._failure(NodeErrorType.INVALID_CONFIG, "inputs", str(exc))
        except Exception as exc:
            self._logger.error(
                "Trial failed on the node", trial_id=trial_id, traceback=traceback.format_exc()
            )
            return self._failure(NodeErrorType.INTERNAL, "run", f"{type(exc).__name__}: {exc}")
        return trial_node_pb2.RunTrialResponse(
            success=True,
            trial_result_json=result.model_dump_json(),
            node_protocol_version=NODE_PROTOCOL_VERSION,
            node_max_message_mb=self._max_message_mb,
        )

    def _place(self, inputs: TrialNodeInputs) -> TrialNodeInputs:
        """Point the inputs' filesystem placement at this node's own mounts."""
        placement: dict[str, Any] = {
            "output_dir": self._output_root / inputs.trial_spec.run_id,
        }
        if self._task_pack_root is not None and inputs.task_pack_dir is not None:
            placement["task_pack_dir"] = self._task_pack_root / inputs.task.task_id
        return inputs.model_copy(update=placement)

    def _failure(self, error_type: str, stage: str, reason: str) -> trial_node_pb2.RunTrialResponse:
        self._logger.error("Trial refused", error_type=error_type, stage=stage, reason=reason)
        return trial_node_pb2.RunTrialResponse(
            success=False,
            error_type=error_type,
            stage=stage,
            reason=reason,
            node_protocol_version=NODE_PROTOCOL_VERSION,
            node_max_message_mb=self._max_message_mb,
        )


def _unresolvable_secret_references(inputs: TrialNodeInputs) -> list[str]:
    """Describe every ``${secret:NAME}`` in *inputs* the node's secret manager cannot resolve.

    Each string field is expanded through :func:`expand_secret_refs` and the
    result discarded, so resolution follows the one sanctioned reference syntax
    and no resolved value outlives the call. A missing name is reported as
    ``${secret:NAME}``; a malformed reference is reported with its location.
    """
    secrets = get_default()
    problems: set[str] = set()
    for where, text in _strings(inputs.model_dump(mode="json"), "inputs"):
        try:
            expand_secret_refs(text, secrets, where=where)
        except UnresolvedReferenceError as exc:
            if exc.names:
                problems.update(f"${{secret:{name}}}" for name in exc.names)
            else:
                problems.add(f"malformed reference at {where}")
    return sorted(problems)


def _strings(node: Any, where: str) -> Iterator[tuple[str, str]]:
    """Yield ``(path, text)`` for every string leaf of a JSON-shaped value."""
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{where}[{index}]")
