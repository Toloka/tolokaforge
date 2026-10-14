"""Canonical lock for the ``local`` dispatch adapter's thread mode.

Thread mode runs each trial in-process on a thread pool, round-tripping the
inputs through JSON exactly as a node would and calling an injected
``run_trial_fn`` in place of the real backend, so no node, Docker daemon or LLM
is involved. These tests lock the serialize/run/return contract, the worker
concurrency bound, and the exception-to-neutral-error mapping — which must match
what the ``grpc_node`` path yields for a real node.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from tests.canonical._factories import make_trial_spec
from tolokaforge.core.dispatch.adapter import DispatchAdapter
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
)
from tolokaforge.core.dispatch.local import LocalDispatchAdapter
from tolokaforge.core.models import TaskConfig, TrialStatus
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    load_dispatch_adapter,
)
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_node import TaskPackDriftError

pytestmark = pytest.mark.canonical


def _result(trial_id: str = "t:0") -> TrialResult:
    from tolokaforge.core.conductor import _default_success_trajectory

    task_id, index = trial_id.rsplit(":", 1)
    return TrialResult.from_trajectory(
        trial_id=trial_id, trajectory=_default_success_trajectory(task_id, int(index))
    )


def _inputs(trial_id: str = "t:0") -> TrialNodeInputs:
    task_id = trial_id.rsplit(":", 1)[0]
    return TrialNodeInputs(
        task=TaskConfig(
            task_id=task_id,
            name=task_id,
            category="test",
            description="d",
            initial_user_message="hi",
        ),
        trial_spec=make_trial_spec(trial_id=trial_id, task_id=task_id),
        grpc_max_message_mb=50,
        runtime="auto",
        conductor="in_process",
    )


def _thread_adapter(run_trial_fn: Callable[[TrialNodeInputs], TrialResult], *, workers: int = 1):
    adapter = LocalDispatchAdapter(mode="thread", workers=workers, run_trial_fn=run_trial_fn)
    adapter.connect()
    return adapter


def test_is_a_dispatch_adapter_and_resolves_from_the_registry() -> None:
    adapter = LocalDispatchAdapter(mode="thread", workers=1)
    assert isinstance(adapter, DispatchAdapter)
    assert load_dispatch_adapter("local") is not None


def test_thread_mode_round_trips_the_inputs_and_returns_the_result() -> None:
    received: list[TrialNodeInputs] = []

    def run(inputs: TrialNodeInputs) -> TrialResult:
        received.append(inputs)
        return _result(inputs.trial_spec.trial_id)

    adapter = _thread_adapter(run)
    original = _inputs("t:0")
    try:
        result = adapter.run_trial_node(original)
    finally:
        adapter.close()

    # The fake ran on an independent, JSON-round-tripped copy equal to the original.
    assert len(received) == 1
    assert received[0] == original
    assert received[0] is not original
    assert result.trial_id == "t:0"
    assert result.trajectory.status is TrialStatus.COMPLETED


def test_thread_mode_bounds_concurrency_to_the_worker_count() -> None:
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Semaphore(0)
    state = {"in_flight": 0, "peak": 0}

    def run(inputs: TrialNodeInputs) -> TrialResult:
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        entered.release()
        assert release.wait(timeout=10)
        with lock:
            state["in_flight"] -= 1
        return _result(inputs.trial_spec.trial_id)

    adapter = _thread_adapter(run, workers=2)
    results: list[TrialResult] = []
    callers = [
        threading.Thread(
            target=lambda i=i: results.append(adapter.run_trial_node(_inputs(f"t:{i}")))
        )
        for i in range(4)
    ]
    try:
        for caller in callers:
            caller.start()
        # Exactly ``workers`` trials enter before any more can; the next two
        # cannot start until these finish.
        assert entered.acquire(timeout=10)
        assert entered.acquire(timeout=10)
        time.sleep(0.2)
        with lock:
            assert state["in_flight"] == 2
        release.set()
        for caller in callers:
            caller.join(timeout=10)
    finally:
        release.set()
        adapter.close()

    assert len(results) == 4
    assert state["peak"] == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ProvisionError(trial_id="t:0", stage="await_ready", reason="no capacity"),
            DispatchProvisionError,
        ),
        (TaskPackDriftError("pack drifted"), DispatchRefused),
        (NotImplementedError("adapter is not node-runnable"), DispatchRefused),
        (UnknownImplementationError("nope", "tolokaforge.conductors", []), DispatchRefused),
        (ValueError("deep in the trial"), DispatchTransient),
        (RuntimeError("something blew up"), DispatchTransient),
    ],
)
def test_thread_mode_maps_each_failure_to_its_neutral_dispatch_error(
    error: Exception, expected: type
) -> None:
    def run(inputs: TrialNodeInputs) -> TrialResult:
        raise error

    adapter = _thread_adapter(run)
    try:
        with pytest.raises(expected):
            adapter.run_trial_node(_inputs())
    finally:
        adapter.close()
