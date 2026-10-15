"""The ``local`` subprocess adapter's control plane, driven with fake workers.

Subprocess mode's control operations — cancel the leased worker, report coarse
lease-derived status, collect the node-written bundle — are exercised here
against the injected ``spawn_fn`` seam the registry tests use, so no Docker
daemon, LLM or real node process is involved. The full Docker-backed run lives
in ``test_local_dispatch_subprocess_e2e`` (Linux CI only).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.dispatch.control import JobHandle, JobState
from tolokaforge.core.dispatch.errors import DispatchUnsupported
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.dispatch.local import LocalDispatchAdapter
from tolokaforge.core.node_registry import SpawningNodeRegistry
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.output_writer import TRAJECTORY_FILENAME
from tolokaforge.core.trial import TrialResult

pytestmark = pytest.mark.integration


class _FakeWorker:
    """A stand-in process that really listens on its port but runs no trial."""

    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.port = int(command[command.index("--port") + 1])
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen()
        self.terminated = False
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

    def kill(self) -> None:  # pragma: no cover - the fake always stops on terminate
        pass


class _Spawner:
    def __init__(self) -> None:
        self.workers: list[_FakeWorker] = []

    def __call__(self, command: list[str], env: dict[str, str]) -> _FakeWorker:
        worker = _FakeWorker(command, env)
        self.workers.append(worker)
        return worker


def _subprocess_adapter(spawner: _Spawner, *, workers: int = 1) -> LocalDispatchAdapter:
    registry = SpawningNodeRegistry(workers=workers, spawn_fn=spawner, connect_timeout_s=5.0)
    grpc_adapter = GrpcNodeDispatchAdapter(registry=registry)
    adapter = LocalDispatchAdapter(
        mode="subprocess", workers=workers, registry=registry, grpc_adapter=grpc_adapter
    )
    adapter.connect()
    return adapter


def _trajectory(task_id: str = "task", trial_index: int = 0):
    from tolokaforge.core.conductor import _default_success_trajectory

    return _default_success_trajectory(task_id, trial_index)


def test_cancel_terminates_the_leased_worker() -> None:
    spawner = _Spawner()
    adapter = _subprocess_adapter(spawner)
    try:
        # Bind the trial to the one worker, then cancel it.
        assert adapter.registry is not None
        adapter.registry.lease("task:0")
        adapter.cancel(JobHandle(trial_id="task:0"))
        assert spawner.workers[0].terminated
    finally:
        adapter.close()


def test_cancel_of_an_unleased_trial_is_a_no_op() -> None:
    spawner = _Spawner()
    adapter = _subprocess_adapter(spawner)
    try:
        adapter.cancel(JobHandle(trial_id="never:0"))  # must not raise
        assert not spawner.workers[0].terminated
    finally:
        adapter.close()


def test_status_is_coarse_running_while_leased_else_unknown() -> None:
    spawner = _Spawner()
    adapter = _subprocess_adapter(spawner)
    try:
        assert adapter.registry is not None and adapter.grpc_adapter is not None
        handle = JobHandle(trial_id="task:0")
        assert adapter.status(handle).state is JobState.UNKNOWN

        # The adapter tracks the live lease on its gRPC delegate.
        adapter.grpc_adapter._leases["task:0"] = adapter.registry.lease("task:0")
        status = adapter.status(handle)
        assert status.state is JobState.RUNNING
        assert "coarse" in status.detail
    finally:
        adapter.close()


def test_fetch_result_reads_the_node_written_bundle() -> None:
    spawner = _Spawner()
    adapter = _subprocess_adapter(spawner)
    try:
        assert adapter.registry is not None
        root = adapter.registry.output_root
        assert root is not None
        trajectory = _trajectory("task", 0)
        trial_dir = Path(root) / "run-xyz" / "trials" / "task" / "0"
        FileArtifactWriter().write_trajectory(trial_dir, trajectory)
        assert (trial_dir / TRAJECTORY_FILENAME).exists()

        result = adapter.fetch_result(JobHandle(trial_id="task:0"))
        assert isinstance(result, TrialResult)
        assert result.trial_id == "task:0"
    finally:
        adapter.close()


def test_fetch_result_without_a_bundle_is_unsupported() -> None:
    spawner = _Spawner()
    adapter = _subprocess_adapter(spawner)
    try:
        with pytest.raises(DispatchUnsupported, match="not retained"):
            adapter.fetch_result(JobHandle(trial_id="missing:0"))
    finally:
        adapter.close()


def test_the_subprocess_bundle_round_trips_a_written_trajectory(tmp_path: Path) -> None:
    # The reader and the writer agree on the on-disk YAML shape.
    trajectory = _trajectory("task", 0)
    trial_dir = tmp_path / "trials" / "task" / "0"
    FileArtifactWriter().write_trajectory(trial_dir, trajectory)
    reloaded = yaml.safe_load((trial_dir / TRAJECTORY_FILENAME).read_text())
    assert reloaded["task_id"] == "task"
