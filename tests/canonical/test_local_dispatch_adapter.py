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
from tolokaforge.core.dispatch.adapter import ControllableDispatchAdapter, DispatchAdapter
from tolokaforge.core.dispatch.control import JobHandle, JobState
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
    DispatchUnsupported,
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
    assert isinstance(adapter, ControllableDispatchAdapter)
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


# ---- Thread-mode control plane ---------------------------------------------


def test_thread_status_runs_then_completes_but_the_result_is_not_retained() -> None:
    release = threading.Event()
    entered = threading.Event()

    def run(inputs: TrialNodeInputs) -> TrialResult:
        entered.set()
        assert release.wait(timeout=10)
        return _result(inputs.trial_spec.trial_id)

    adapter = _thread_adapter(run)
    handle = JobHandle(trial_id="t:0")
    result_box: list[TrialResult] = []
    caller = threading.Thread(target=lambda: result_box.append(adapter.run_trial_node(_inputs())))
    try:
        caller.start()
        assert entered.wait(timeout=10)
        assert adapter.status(handle).state is JobState.RUNNING
        release.set()
        caller.join(timeout=10)
        assert adapter.status(handle).state is JobState.COMPLETED
        # The result was returned live from run_trial_node; thread mode keeps no
        # durable copy, so fetch_result is the neutral "not retained" error while
        # status still answers from the kept JobState.
        assert result_box[0].trial_id == "t:0"
        with pytest.raises(DispatchUnsupported, match="not retained"):
            adapter.fetch_result(handle)
        assert adapter.status(handle).state is JobState.COMPLETED
    finally:
        release.set()
        adapter.close()


def test_a_completed_thread_job_retains_no_future_or_result() -> None:
    # A terminal job keeps only its small JobState; the Future (which holds the
    # graded TrialResult in future._result) is dropped so an N-trial run does not
    # pin every trajectory, message history and tool log for the adapter's life.
    adapter = _thread_adapter(lambda inputs: _result(inputs.trial_spec.trial_id))
    try:
        adapter.run_trial_node(_inputs("t:0"))
        job = adapter._jobs["t:0"]
        assert job.state is JobState.COMPLETED
        assert job.future is None
    finally:
        adapter.close()


def test_a_failed_thread_job_retains_no_future() -> None:
    def run(inputs: TrialNodeInputs) -> TrialResult:
        raise RuntimeError("boom")

    adapter = _thread_adapter(run)
    try:
        with pytest.raises(DispatchTransient):
            adapter.run_trial_node(_inputs("t:0"))
        job = adapter._jobs["t:0"]
        assert job.state is JobState.FAILED
        assert job.future is None
    finally:
        adapter.close()


def test_thread_status_is_pending_while_queued_then_running_once_started() -> None:
    occupy_entered = threading.Event()
    occupy_release = threading.Event()

    def run(inputs: TrialNodeInputs) -> TrialResult:
        if inputs.trial_spec.trial_id == "t:0":
            occupy_entered.set()
            assert occupy_release.wait(timeout=10)
        return _result(inputs.trial_spec.trial_id)

    # One worker: the second trial is queued (PENDING) until the first frees it,
    # then flips to RUNNING when a worker picks it up.
    adapter = _thread_adapter(run, workers=1)
    occupier = threading.Thread(target=lambda: adapter.run_trial_node(_inputs("t:0")))
    queued = threading.Thread(target=lambda: _swallow(adapter.run_trial_node, _inputs("t:1")))
    handle = JobHandle(trial_id="t:1")
    try:
        occupier.start()
        assert occupy_entered.wait(timeout=10)
        queued.start()
        _wait_until(lambda: adapter.status(handle).state is JobState.PENDING)
        # Still queued behind the single occupied worker.
        assert adapter.status(handle).state is JobState.PENDING
        occupy_release.set()
        _wait_until(lambda: adapter.status(handle).state is JobState.COMPLETED)
    finally:
        occupy_release.set()
        occupier.join(timeout=10)
        queued.join(timeout=10)
        adapter.close()


def test_thread_status_is_failed_after_the_trial_raises() -> None:
    def run(inputs: TrialNodeInputs) -> TrialResult:
        raise RuntimeError("blew up")

    adapter = _thread_adapter(run)
    try:
        with pytest.raises(DispatchTransient):
            adapter.run_trial_node(_inputs())
        handle = JobHandle(trial_id="t:0")
        assert adapter.status(handle).state is JobState.FAILED
        # Thread mode retains no result, so fetch_result is the neutral "not
        # retained" error; status still reports the terminal FAILED state.
        with pytest.raises(DispatchUnsupported, match="not retained"):
            adapter.fetch_result(handle)
    finally:
        adapter.close()


def test_thread_status_is_unknown_for_a_trial_that_never_ran() -> None:
    adapter = _thread_adapter(lambda inputs: _result(inputs.trial_spec.trial_id))
    try:
        assert adapter.status(JobHandle(trial_id="never:0")).state is JobState.UNKNOWN
    finally:
        adapter.close()


def test_thread_cancel_of_an_unstarted_job_marks_it_cancelled() -> None:
    occupy_entered = threading.Event()
    occupy_release = threading.Event()

    def run(inputs: TrialNodeInputs) -> TrialResult:
        if inputs.trial_spec.trial_id == "t:0":
            occupy_entered.set()
            assert occupy_release.wait(timeout=10)
        return _result(inputs.trial_spec.trial_id)

    # One worker: the second trial cannot start until the first frees it.
    adapter = _thread_adapter(run, workers=1)
    occupier = threading.Thread(target=lambda: adapter.run_trial_node(_inputs("t:0")))
    queued = threading.Thread(target=lambda: _swallow(adapter.run_trial_node, _inputs("t:1")))
    try:
        occupier.start()
        assert occupy_entered.wait(timeout=10)
        queued.start()
        # Wait until the queued job is in the table, still PENDING behind the one
        # occupied worker so cancelling it genuinely stops it before it starts.
        handle = JobHandle(trial_id="t:1")
        _wait_until(lambda: adapter.status(handle).state is JobState.PENDING)

        adapter.cancel(handle)
        assert adapter.status(handle).state is JobState.CANCELLED
    finally:
        occupy_release.set()
        occupier.join(timeout=10)
        queued.join(timeout=10)
        adapter.close()


def test_thread_fetch_result_is_unsupported_because_nothing_is_retained() -> None:
    adapter = _thread_adapter(lambda inputs: _result(inputs.trial_spec.trial_id))
    try:
        with pytest.raises(DispatchUnsupported, match="not retained"):
            adapter.fetch_result(JobHandle(trial_id="missing:0"))
    finally:
        adapter.close()


def _swallow(fn: Callable[[TrialNodeInputs], TrialResult], inputs: TrialNodeInputs) -> None:
    try:
        fn(inputs)
    except BaseException:  # noqa: BLE001 — the cancelled job's future reports back however it may
        pass


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
