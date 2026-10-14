"""Node registry — hands each trial the address of a trial node to dispatch to.

A runtime backend that executes trials on remote nodes leases one node per trial
through :class:`NodeRegistry`. The lease is the only thing that binds a trial to
a node address; releasing it frees the node for the next trial.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB
from tolokaforge.secrets import container_secrets_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerLease:
    """A trial's claim on one trial node."""

    node_id: str
    runner_address: str


@runtime_checkable
class NodeRegistry(Protocol):
    """The set of trial nodes a run may dispatch trials to.

    Implementations must be thread-safe: trials lease and release concurrently.
    """

    def register_node(self, node_id: str, runner_address: str) -> None:
        """Add a node, or update the address of an existing one."""
        ...

    def lease(self, trial_id: str) -> RunnerLease:
        """Bind ``trial_id`` to a free node, blocking until one frees.

        Idempotent per trial: a trial that already holds a lease gets it back.

        Raises:
            TimeoutError: no node became free within the registry's lease timeout.
        """
        ...

    def release(self, trial_id: str) -> None:
        """Free the node ``trial_id`` holds. A trial with no lease is a no-op."""
        ...

    def has_nodes(self) -> bool:
        """Whether at least one node is registered."""
        ...


class InMemoryNodeRegistry:
    """Process-local :class:`NodeRegistry` seeded from a list of ``host:port`` addresses.

    Each node serves one trial at a time, so the node count bounds trial
    concurrency: :meth:`lease` blocks while every node is leased.

    ``lease_timeout_s`` is how long :meth:`lease` waits for a free node before
    raising :class:`TimeoutError`.
    """

    def __init__(self, addresses: Iterable[str] = (), *, lease_timeout_s: float = 300.0) -> None:
        self._lease_timeout_s = lease_timeout_s
        self._cond = threading.Condition()
        self._nodes: dict[str, str] = {}
        self._leases: dict[str, RunnerLease] = {}
        for index, address in enumerate(addresses):
            self._nodes[f"node-{index}"] = address

    def register_node(self, node_id: str, runner_address: str) -> None:
        with self._cond:
            self._nodes[node_id] = runner_address
            self._cond.notify()

    def lease(self, trial_id: str) -> RunnerLease:
        deadline = time.monotonic() + self._lease_timeout_s
        with self._cond:
            while True:
                held = self._leases.get(trial_id)
                if held is not None:
                    return held
                leased_nodes = {lease.node_id for lease in self._leases.values()}
                for node_id, address in self._nodes.items():
                    if node_id in leased_nodes:
                        continue
                    lease = RunnerLease(node_id=node_id, runner_address=address)
                    self._leases[trial_id] = lease
                    return lease
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no node became free within {self._lease_timeout_s:g}s "
                        f"for trial {trial_id!r} ({len(self._nodes)} node(s) registered)"
                    )
                self._cond.wait(timeout=remaining)

    def release(self, trial_id: str) -> None:
        with self._cond:
            if self._leases.pop(trial_id, None) is not None:
                self._cond.notify()

    def has_nodes(self) -> bool:
        with self._cond:
            return bool(self._nodes)

    def _reset_nodes(self) -> None:
        """Drop every registered node and lease; wake any blocked leaser."""
        with self._cond:
            self._nodes.clear()
            self._leases.clear()
            self._cond.notify_all()


SpawnFn = Callable[[list[str], Mapping[str, str]], "subprocess.Popen[bytes]"]
"""Starts one trial-node worker: ``(command, env) -> process``. Tests inject a
fake in place of a real :class:`subprocess.Popen`."""

FreePortFn = Callable[[], int]
"""Returns an ephemeral localhost port to bind a worker on."""

_WORKER_START_TIMEOUT_FLOOR_S = 30.0
"""Lower bound on a worker's health-wait budget, so a short per-trial connect
budget does not starve a cold node process of start-up time."""


def _free_port() -> int:
    """Return an OS-assigned free localhost port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_listen(port: int, process: subprocess.Popen[bytes], timeout: float) -> None:
    """Block until *port* accepts a connection, failing fast if *process* exits.

    Raises:
        RuntimeError: the worker exited before it listened, or did not listen
            within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the local trial-node worker exited early with code {process.returncode} "
                f"before listening on port {port}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(
        f"the local trial-node worker did not listen on port {port} within {timeout:g}s"
    )


def _spawn_trial_node(command: list[str], env: Mapping[str, str]) -> subprocess.Popen[bytes]:
    """Start one ``python -m tolokaforge.trial_node`` worker process."""
    return subprocess.Popen(command, env=dict(env))


class SpawningNodeRegistry(InMemoryNodeRegistry):
    """A :class:`NodeRegistry` that spawns its own local trial-node worker processes.

    :meth:`start` launches ``workers`` ``python -m tolokaforge.trial_node``
    processes, each on its own ephemeral localhost port, waits for each to
    listen, and registers it; :meth:`lease` / :meth:`release` / :meth:`has_nodes`
    are inherited unchanged, so a :class:`~tolokaforge.core.dispatch.grpc_node.GrpcNodeDispatchAdapter`
    drives spawned workers exactly as it drives configured addresses. Every
    worker writes its trial artifacts under one adapter-owned temporary root,
    discarded on :meth:`close`.

    ``spawn_fn``, ``python_executable`` and ``free_port_fn`` are injection seams:
    tests spawn fakes instead of real processes.
    """

    def __init__(
        self,
        *,
        workers: int,
        grpc_max_message_mb: int = DEFAULT_MAX_MESSAGE_MB,
        lease_timeout_s: float = 300.0,
        connect_timeout_s: float = 30.0,
        retry_interval_s: float = 1.0,
        spawn_fn: SpawnFn = _spawn_trial_node,
        python_executable: str = sys.executable,
        free_port_fn: FreePortFn = _free_port,
    ) -> None:
        super().__init__(lease_timeout_s=lease_timeout_s)
        self._workers = workers
        self._grpc_max_message_mb = grpc_max_message_mb
        self._connect_timeout_s = connect_timeout_s
        self._retry_interval_s = retry_interval_s
        self._spawn_fn = spawn_fn
        self._python_executable = python_executable
        self._free_port_fn = free_port_fn
        self._procs: list[subprocess.Popen[bytes]] = []
        self._output_root: Path | None = None
        self._started = False

    def start(self) -> None:
        """Spawn and health-wait every worker, then register each one.

        Called from the adapter's ``connect`` so a spawn or start-up failure
        surfaces there. Tears down any worker already started if a later one
        fails, so a partial start leaks no processes.

        Raises:
            RuntimeError: a worker exited before, or never began, listening.
        """
        if self._started:
            return
        self._started = True
        self._output_root = Path(tempfile.mkdtemp(prefix="tolokaforge-local-dispatch-"))
        budget = max(self._connect_timeout_s, _WORKER_START_TIMEOUT_FLOOR_S)
        try:
            for index in range(self._workers):
                port = self._free_port_fn()
                command = [
                    self._python_executable,
                    "-m",
                    "tolokaforge.trial_node",
                    "--port",
                    str(port),
                    "--output-root",
                    str(self._output_root),
                ]
                env = {
                    **os.environ,
                    **container_secrets_env(),
                    "TRIAL_NODE_PORT": str(port),
                    "TOLOKAFORGE_GRPC_MAX_MESSAGE_MB": str(self._grpc_max_message_mb),
                    "EXECUTOR_ADDRESS": os.environ.get("EXECUTOR_ADDRESS", "localhost:50051"),
                }
                process = self._spawn_fn(command, env)
                self._procs.append(process)
                _wait_for_listen(port, process, budget)
                self.register_node(f"local-{index}", f"127.0.0.1:{port}")
                logger.debug("Started local trial-node worker local-%d on port %d", index, port)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Terminate every worker and discard the temp output root. Idempotent."""
        for process in self._procs:
            _terminate(process)
        self._procs.clear()
        self._reset_nodes()
        if self._output_root is not None:
            shutil.rmtree(self._output_root, ignore_errors=True)
            self._output_root = None
        self._started = False


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop one worker: SIGTERM, wait, then SIGKILL if it outlasts the grace period."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


__all__ = [
    "FreePortFn",
    "InMemoryNodeRegistry",
    "NodeRegistry",
    "RunnerLease",
    "SpawnFn",
    "SpawningNodeRegistry",
]
