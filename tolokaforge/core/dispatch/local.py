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

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from tolokaforge.core.dispatch.control import JobHandle, JobState, JobStatus
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
    DispatchUnsupported,
)
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.node_registry import SpawningNodeRegistry
from tolokaforge.core.output.artifacts import read_report_trajectory
from tolokaforge.core.output_writer import TRAJECTORY_FILENAME
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_node import run_trial_from_inputs
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.trial_node.classify import classify_trial_exception
from tolokaforge.trial_node.protocol import NodeErrorType

logger = logging.getLogger(__name__)

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
class _ThreadJob:
    """One in-process trial's table entry: its future and current state.

    Only the small ``state`` is retained, never the trial's full ``TrialResult``.
    The default synchronous flow consumes the live ``run_trial_node`` return, so
    keeping each graded result (its trajectory, message history and tool log) for
    the adapter's whole lifetime would pin an N-trial run's worth of memory for
    nothing.
    """

    future: Future[TrialResult]
    state: JobState


@dataclass
class LocalDispatchAdapter:
    """:class:`~tolokaforge.core.dispatch.adapter.ControllableDispatchAdapter` that runs trials locally.

    ``mode`` picks the substrate: ``thread`` runs each trial in-process on
    :attr:`_pool`; ``subprocess`` delegates to :attr:`grpc_adapter` over
    :attr:`registry`'s spawned worker processes. The factory builds the registry
    and gRPC adapter for ``subprocess`` mode; ``thread`` mode builds its pool on
    :meth:`connect`.

    The control-plane operations dispatch to the active mode. ``thread`` keeps an
    in-process job table (:attr:`_jobs`) so status is real and pre-start cancel
    genuine; a running in-process thread cannot be force-killed, so cancelling one
    is a best-effort no-op. ``subprocess`` cancels by killing the leased worker
    process, reports coarse lease-derived status, and collects the node-written
    bundle.

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
    _jobs: dict[str, _ThreadJob] = field(default_factory=dict, init=False)
    _jobs_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

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
        trial_id = inputs.trial_spec.trial_id
        # Round-trip the inputs through JSON so a threaded trial sees exactly the
        # serialized value a node would, not a shared in-memory object.
        payload = inputs.model_dump_json()
        # Submit and record PENDING under one lock so a status read never races a
        # worker that starts the instant the future is submitted. The worker body
        # flips PENDING to RUNNING when it picks the job up, so status tells a
        # queued job apart from a running one.
        with self._jobs_lock:
            future = self._pool.submit(self._run_one, trial_id, payload)
            self._jobs[trial_id] = _ThreadJob(future=future, state=JobState.PENDING)
        try:
            result = future.result()
        except BaseException:
            # Only a job still RUNNING becomes FAILED: a job cancelled before it
            # started keeps its CANCELLED state when its future reports back.
            with self._jobs_lock:
                job = self._jobs.get(trial_id)
                if job is not None and job.state is JobState.RUNNING:
                    job.state = JobState.FAILED
            raise
        with self._jobs_lock:
            job = self._jobs.get(trial_id)
            if job is not None:
                job.state = JobState.COMPLETED
        return result

    def _run_one(self, trial_id: str, payload: str) -> TrialResult:
        # A job only reaches here once a pool worker has picked it up: flip it to
        # RUNNING. Guard on PENDING so a pre-start cancel (which set CANCELLED) is
        # not clobbered.
        with self._jobs_lock:
            job = self._jobs.get(trial_id)
            if job is not None and job.state is JobState.PENDING:
                job.state = JobState.RUNNING
        rebuilt = TrialNodeInputs.model_validate_json(payload)
        try:
            return self.run_trial_fn(rebuilt)
        except Exception as exc:
            raise _dispatch_error(*classify_trial_exception(exc)) from exc

    # ---- Control plane ----

    def cancel(self, handle: JobHandle) -> None:
        """Request cancellation of the job the handle names. Idempotent.

        ``thread`` mode cancels a job that has not started; a running in-process
        thread cannot be force-killed, so cancelling one is a logged best-effort
        no-op. ``subprocess`` mode kills the leased worker process outright.
        """
        if self.mode == "thread":
            self._cancel_thread(handle)
            return
        assert self.registry is not None  # built by the factory for subprocess mode
        if not self.registry.terminate_worker_for_trial(handle.trial_id):
            logger.debug("No leased worker to cancel for trial %s", handle.trial_id)

    def status(self, handle: JobHandle) -> JobStatus:
        """Return the job's current state; an unknown id is ``JobState.UNKNOWN``."""
        if self.mode == "thread":
            with self._jobs_lock:
                job = self._jobs.get(handle.trial_id)
            if job is None:
                return JobStatus(
                    state=JobState.UNKNOWN,
                    detail=f"no in-process job for {handle.trial_id}",
                )
            return JobStatus(state=job.state)
        assert self.grpc_adapter is not None  # built by the factory for subprocess mode
        # Coarse: the adapter knows only that it holds a lease for the trial, not
        # the worker-side job state.
        if self.grpc_adapter.has_active_lease(handle.trial_id):
            return JobStatus(
                state=JobState.RUNNING,
                detail="coarse: the adapter holds a lease for this trial; node-side state is not queried",
            )
        return JobStatus(state=JobState.UNKNOWN, detail="no lease held for this trial")

    def fetch_result(self, handle: JobHandle) -> TrialResult:
        """Return the job's graded result, or raise ``DispatchUnsupported``.

        ``thread`` mode is in-process and ephemeral: it returns each result live
        from ``run_trial_node`` and retains no durable copy, so there is nothing
        to re-read after the fact and this always raises the neutral "not
        retained" error. ``status`` still answers from the kept ``JobState``.
        ``subprocess`` mode reads the node-written bundle under the worker output
        root.
        """
        if self.mode == "thread":
            raise DispatchUnsupported(
                "Unsupported",
                "",
                f"in-process result returned live; not retained for {handle.trial_id}",
            )
        return self._fetch_result_subprocess(handle.trial_id)

    def _cancel_thread(self, handle: JobHandle) -> None:
        with self._jobs_lock:
            job = self._jobs.get(handle.trial_id)
            if job is None:
                return
            if job.future.cancel():
                job.state = JobState.CANCELLED
                return
        logger.info(
            "Cancel of running in-process trial %s is a no-op; a thread cannot be force-killed",
            handle.trial_id,
        )

    def _fetch_result_subprocess(self, trial_id: str) -> TrialResult:
        assert self.registry is not None  # built by the factory for subprocess mode
        root = self.registry.output_root
        if root is None:
            raise DispatchUnsupported("Unsupported", "", f"result not retained for {trial_id}")
        task_id, index = trial_id.rsplit(":", 1)
        # Normally the output root holds a single per-run subdir. If more than one
        # run subdir carries this trial the choice is not safe to make silently —
        # the lexicographic first could be a stale earlier run — so refuse rather
        # than return a possibly-wrong result.
        matches = [
            run_dir / "trials" / task_id / index
            for run_dir in sorted(p for p in root.iterdir() if p.is_dir())
            if (run_dir / "trials" / task_id / index / TRAJECTORY_FILENAME).exists()
        ]
        if not matches:
            raise DispatchUnsupported("Unsupported", "", f"result not retained for {trial_id}")
        if len(matches) > 1:
            raise DispatchUnsupported(
                "Unsupported", "", f"ambiguous bundle location for {trial_id}"
            )
        # The node writes grade / metrics / tool_log to separate sidecars; the
        # report reader merges them back into one graded trajectory. A corrupt or
        # truncated bundle (unreadable YAML, a schema mismatch, a missing required
        # sidecar) must cross the seam as the neutral control-plane error, not as a
        # raw YAML / validation / OS exception.
        try:
            trajectory = read_report_trajectory(matches[0])
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise DispatchUnsupported(
                "Unsupported", "", f"corrupt or incomplete bundle for {trial_id}"
            ) from exc
        return TrialResult.from_trajectory(trial_id=trial_id, trajectory=trajectory)


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


def _parse_config(config: Mapping[str, object]) -> _LocalConfig:
    try:
        return _LocalConfig.model_validate(dict(config))
    except ValidationError as exc:
        raise ValueError(
            "The 'local' dispatch adapter config is invalid; under "
            "orchestrator.dispatch.config set 'mode' to 'thread' or 'subprocess' and "
            f"'workers' to a positive integer. {exc}"
        ) from exc
