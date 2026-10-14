"""``LocalDispatchAdapter`` — run each whole trial on this machine.

The ``local`` adapter is the native, zero-dependency dispatch path and the
single-box test bed for distributed dispatch. It has two modes:

* ``thread`` runs each trial in-process on a
  :class:`~concurrent.futures.ThreadPoolExecutor`, calling
  :func:`~tolokaforge.core.trial_node.run_trial_from_inputs` directly. It spawns
  no process and opens no socket; concurrency is the pool's worker count.
* ``subprocess`` spawns local ``python -m tolokaforge.trial_node`` workers
  through a :class:`~tolokaforge.core.node_registry.SpawningNodeRegistry` and
  dispatches to them with a
  :class:`~tolokaforge.core.dispatch.grpc_node.GrpcNodeDispatchAdapter`, so the
  whole gRPC dispatch path runs end to end against local processes with no
  external service.

Both modes report failures in the transport-neutral vocabulary of
:mod:`tolokaforge.core.dispatch.errors`, and both run the identical trial unit,
so a run reads the same whichever mode it picks.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError

from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
)
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.node_registry import SpawningNodeRegistry
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_node import run_trial_from_inputs
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.trial_node.classify import classify_trial_exception
from tolokaforge.trial_node.protocol import NodeErrorType

if TYPE_CHECKING:
    from tolokaforge.core.dispatch.errors import _DispatchFailure
    from tolokaforge.core.plugin_registry import DispatchAdapterBuildContext

RunTrialFn = Callable[[TrialNodeInputs], TrialResult]
"""Runs one trial from inputs; thread mode defaults it to ``run_trial_from_inputs``."""

__all__ = [
    "LocalDispatchAdapter",
    "local_dispatch_adapter_factory",
]


class _LocalConfig(BaseModel):
    """The ``local`` adapter's own options block (``orchestrator.dispatch.config``)."""

    model_config = {"extra": "forbid"}

    mode: Literal["thread", "subprocess"] = "thread"
    """``thread`` runs each trial in-process on a thread pool; ``subprocess``
    spawns local trial-node worker processes and dispatches to them over gRPC."""

    workers: int = Field(default=1, gt=0)
    """Trial concurrency. ``thread``: the pool's ``max_workers``. ``subprocess``:
    the number of spawned worker processes, each serving one trial at a time."""

    lease_timeout_s: float = Field(default=300.0, gt=0.0)
    """``subprocess`` only: how long a trial waits for a free worker before the
    run fails. ``thread`` mode does not lease, so this does not apply to it."""

    grpc_max_message_mb: int = Field(default=DEFAULT_MAX_MESSAGE_MB, gt=0)
    """``subprocess`` only: the gRPC message bound set on each worker and used by
    each worker client, so both ends share one limit and the skew pre-flight
    passes. ``thread`` mode opens no channel, so this does not apply to it."""


@dataclass
class LocalDispatchAdapter:
    """:class:`~tolokaforge.core.dispatch.adapter.DispatchAdapter` that runs trials locally.

    ``mode`` picks the substrate: ``thread`` runs each trial in-process on
    :attr:`_pool`; ``subprocess`` delegates to :attr:`grpc_adapter` over
    :attr:`registry`'s spawned worker processes. The factory builds the registry
    and gRPC adapter for ``subprocess`` mode; ``thread`` mode builds its pool on
    :meth:`connect`.

    ``run_trial_fn`` is the thread-mode seam: tests inject a fake in place of
    :func:`~tolokaforge.core.trial_node.run_trial_from_inputs` so no backend,
    Docker daemon or LLM is involved.
    """

    mode: Literal["thread", "subprocess"]
    workers: int
    run_trial_fn: RunTrialFn = run_trial_from_inputs
    registry: SpawningNodeRegistry | None = None
    grpc_adapter: GrpcNodeDispatchAdapter | None = None
    _pool: ThreadPoolExecutor | None = field(default=None, init=False)

    # ---- Run-level lifecycle ----

    def connect(self) -> None:
        """Build the thread pool, or spawn and health-wait the worker processes."""
        if self.mode == "thread":
            self._pool = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="local-dispatch"
            )
            return
        assert self.registry is not None  # built by the factory for subprocess mode
        self.registry.start()

    def close(self) -> None:
        """Release the pool, or tear down the gRPC adapter and worker processes. Idempotent."""
        if self.mode == "thread":
            if self._pool is not None:
                self._pool.shutdown(wait=True)
                self._pool = None
            return
        if self.grpc_adapter is not None:
            self.grpc_adapter.close()
        if self.registry is not None:
            self.registry.close()

    # ---- Whole-trial dispatch ----

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        """Run one whole trial locally and return its graded result.

        Raises:
            DispatchProvisionError: the trial's substrate failed to come up.
            DispatchRefused: the trial was refused for a reason a retry cannot change.
            DispatchTransient: the trial failed for a reason a retry may clear.
        """
        if self.mode == "thread":
            return self._run_on_thread(inputs)
        assert self.grpc_adapter is not None  # built by the factory for subprocess mode
        return self.grpc_adapter.run_trial_node(inputs)

    def _run_on_thread(self, inputs: TrialNodeInputs) -> TrialResult:
        if self._pool is None:
            raise RuntimeError("LocalDispatchAdapter is not connected; call connect().")
        # Round-trip the inputs through JSON so a threaded trial sees exactly the
        # serialized value a node would, not a shared in-memory object.
        payload = inputs.model_dump_json()
        return self._pool.submit(self._run_one, payload).result()

    def _run_one(self, payload: str) -> TrialResult:
        rebuilt = TrialNodeInputs.model_validate_json(payload)
        try:
            return self.run_trial_fn(rebuilt)
        except Exception as exc:
            raise _dispatch_error(*classify_trial_exception(exc)) from exc


def _dispatch_error(error_type: str, stage: str, reason: str) -> _DispatchFailure:
    """Map a classified node error to its neutral dispatch exception.

    The same mapping :func:`~tolokaforge.core.dispatch.grpc_node._result_from`
    applies to a node's reply, so thread mode and a real node agree on what each
    failure becomes.
    """
    if error_type == NodeErrorType.PROVISION_ERROR:
        return DispatchProvisionError(error_type, stage, reason)
    if error_type in (NodeErrorType.NODE_BUSY, NodeErrorType.INTERNAL):
        return DispatchTransient(error_type, stage, reason)
    return DispatchRefused(error_type, stage, reason)


def local_dispatch_adapter_factory(ctx: DispatchAdapterBuildContext) -> LocalDispatchAdapter:
    """Build a :class:`LocalDispatchAdapter` from a dispatch build context.

    Reads the mode, worker count, lease timeout and gRPC message bound from
    ``ctx.config``. In ``subprocess`` mode the worker start-up budget and the
    per-trial client connect budget both come from ``ctx.connect_timeout_s`` /
    ``ctx.connect_retry_interval_s``.
    """
    config = _parse_config(ctx.config)
    if config.mode == "thread":
        return LocalDispatchAdapter(mode="thread", workers=config.workers)
    registry = SpawningNodeRegistry(
        workers=config.workers,
        grpc_max_message_mb=config.grpc_max_message_mb,
        lease_timeout_s=config.lease_timeout_s,
        connect_timeout_s=ctx.connect_timeout_s,
        retry_interval_s=ctx.connect_retry_interval_s,
    )
    grpc_adapter = GrpcNodeDispatchAdapter(
        registry=registry,
        grpc_max_message_mb=config.grpc_max_message_mb,
        connect_timeout_s=ctx.connect_timeout_s,
        retry_interval_s=ctx.connect_retry_interval_s,
    )
    return LocalDispatchAdapter(
        mode="subprocess",
        workers=config.workers,
        registry=registry,
        grpc_adapter=grpc_adapter,
    )


def _parse_config(config: object) -> _LocalConfig:
    try:
        return _LocalConfig.model_validate(dict(config))  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ValueError(
            "The 'local' dispatch adapter config is invalid; under "
            "orchestrator.dispatch.config set 'mode' to 'thread' or 'subprocess' and "
            f"'workers' to a positive integer. {exc}"
        ) from exc
