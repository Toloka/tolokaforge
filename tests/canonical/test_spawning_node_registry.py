"""Canonical lock for :class:`SpawningNodeRegistry`.

The registry spawns one trial-node worker per requested worker, each on its own
port, and tears every one down on close. A fake spawner stands in for a real
``python -m tolokaforge.trial_node`` process — it opens a listening socket so the
registry's real health-wait passes — so no node, Docker daemon or LLM is
involved.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tolokaforge.core.node_registry import SpawningNodeRegistry

pytestmark = pytest.mark.canonical


class _FakeWorker:
    """A stand-in process that really listens on its port but runs no trial."""

    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.env = env
        self.port = int(command[command.index("--port") + 1])
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen()
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    @property
    def returncode(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self._sock.close()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


class _Spawner:
    def __init__(self) -> None:
        self.workers: list[_FakeWorker] = []

    def __call__(self, command: list[str], env: dict[str, str]) -> _FakeWorker:
        worker = _FakeWorker(command, env)
        self.workers.append(worker)
        return worker


def test_start_spawns_one_worker_per_worker_on_unique_ports() -> None:
    spawner = _Spawner()
    registry = SpawningNodeRegistry(
        workers=3, grpc_max_message_mb=13, spawn_fn=spawner, connect_timeout_s=5.0
    )

    registry.start()
    try:
        assert len(spawner.workers) == 3
        ports = [worker.port for worker in spawner.workers]
        assert len(set(ports)) == 3
        assert registry.has_nodes()
        # Each worker learns the shared message bound on both ends.
        assert all(w.env["TOLOKAFORGE_GRPC_MAX_MESSAGE_MB"] == "13" for w in spawner.workers)
        # The registry hands out one leased node per trial up to the worker count.
        leased = {registry.lease(f"t:{i}").runner_address for i in range(3)}
        assert leased == {f"127.0.0.1:{port}" for port in ports}
    finally:
        registry.close()

    assert all(worker.terminated for worker in spawner.workers)


def test_start_is_idempotent_and_spawns_once() -> None:
    spawner = _Spawner()
    registry = SpawningNodeRegistry(workers=2, spawn_fn=spawner)

    registry.start()
    registry.start()
    try:
        assert len(spawner.workers) == 2
    finally:
        registry.close()


def test_close_discards_the_worker_output_root() -> None:
    spawner = _Spawner()
    registry = SpawningNodeRegistry(workers=1, spawn_fn=spawner)

    registry.start()
    output_root = registry._output_root
    assert output_root is not None and Path(output_root).is_dir()

    registry.close()

    assert not Path(output_root).exists()
    # Idempotent: a second close is a no-op.
    registry.close()


def test_close_terminates_workers_and_frees_the_registry() -> None:
    spawner = _Spawner()
    registry = SpawningNodeRegistry(workers=1, spawn_fn=spawner)
    registry.start()

    registry.close()

    assert spawner.workers[0].terminated
    assert not registry.has_nodes()


def test_a_worker_that_never_listens_fails_start_and_tears_down() -> None:
    class _DeadWorker:
        def __init__(self, command: list[str], env: dict[str, str]) -> None:
            self.returncode = 1
            self.terminated = False

        def poll(self) -> int:
            return 1  # already exited

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 1

        def kill(self) -> None:
            pass

    registry = SpawningNodeRegistry(
        workers=1, spawn_fn=lambda command, env: _DeadWorker(command, env), connect_timeout_s=0.5
    )

    with pytest.raises(RuntimeError, match="exited early"):
        registry.start()
    assert not registry.has_nodes()
