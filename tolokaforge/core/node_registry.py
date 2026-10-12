"""Node registry — hands each trial the address of a runner endpoint to dispatch to.

A runtime backend that executes trials on remote nodes leases one node per trial
through :class:`NodeRegistry`. The lease is the only thing that binds a trial to
a runner address; releasing it frees the node for the next trial.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RunnerLease:
    """A trial's claim on one node's runner endpoint."""

    node_id: str
    runner_address: str


@runtime_checkable
class NodeRegistry(Protocol):
    """The set of runner nodes a run may dispatch trials to."""

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


__all__ = ["InMemoryNodeRegistry", "NodeRegistry", "RunnerLease"]
