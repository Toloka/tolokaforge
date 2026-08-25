"""``QueueTrialGrader`` — the plug-in seam's fourth registered impl.

Locks the throughput property that ADR-0038 built the seam to buy:
- Producer (the orchestrator's worker) publishes a grade job and blocks on
  a future — but the *broker* holds the job, not the worker.
- Consumers (grader workers) pull from the queue in parallel; total wall-
  clock on N jobs scales as ``N / consumer_count`` rather than as ``N``.

The in-memory :class:`InMemoryGradeBroker` is the reference impl the tests
exercise. A Redis Streams (or RabbitMQ / SQS) backend plugs behind the
same :class:`GradeBroker` Protocol; the seam property is proved here.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.models import Grade, GradeComponents, TerminationReason, TrialStatus
from tolokaforge.core.plugin_registry import TrialGraderContext, load_trial_grader
from tolokaforge.core.trial_grader import (
    GradingFailedError,
    QueueTrialGrader,
    TrialGrader,
    queue_trial_grader_factory,
)
from tolokaforge.grader.queue import (
    GradeBroker,
    GradeResult,
    InMemoryGradeBroker,
)

pytestmark = pytest.mark.canonical


def _make_grader(
    broker: GradeBroker | None = None,
    timeout_s: float = 5.0,
    runner_substrate_address: str = "runner:50051",
) -> tuple[QueueTrialGrader, GradeBroker]:
    broker = broker if broker is not None else InMemoryGradeBroker()
    grader = QueueTrialGrader(
        broker=broker,
        logger=MagicMock(),
        runner_substrate_address=runner_substrate_address,
        timeout_s=timeout_s,
    )
    return grader, broker


def _canned_verdict() -> Grade:
    return Grade(
        binary_pass=True,
        score=0.8,
        components=GradeComponents(llm_judge=0.8),
        reasons="stub queue-worker verdict",
    )


class _CannedWorker(threading.Thread):
    """Consumer thread that answers every queued job with the same verdict."""

    def __init__(
        self,
        broker: GradeBroker,
        verdict: Grade | None,
        per_job_delay: float = 0.0,
        error_by_trial: dict[str, str] | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.broker = broker
        self.verdict = verdict
        self.per_job_delay = per_job_delay
        self.error_by_trial = error_by_trial or {}
        self._stop_evt = threading.Event()
        self.completed: list[str] = []

    def run(self) -> None:
        from tolokaforge.grader.queue import BrokerClosed

        while not self._stop_evt.is_set():
            try:
                job = self.broker.next_job(timeout=0.05)
            except BrokerClosed:
                # Broker shut down cleanly — stop consuming.
                return
            if job is None:
                continue
            if self.per_job_delay:
                time.sleep(self.per_job_delay)
            error = self.error_by_trial.get(job.trial_id, "")
            self.broker.publish_result(
                GradeResult(job_id=job.job_id, grade=self.verdict, error=error)
            )
            self.completed.append(job.trial_id)

    def stop_worker(self) -> None:
        self._stop_evt.set()


class TestProtocolContract:
    def test_satisfies_trial_grader_protocol(self) -> None:
        grader, _ = _make_grader()
        assert isinstance(grader, TrialGrader)


class TestBasicDispatch:
    def test_grade_returns_worker_verdict(self) -> None:
        grader, broker = _make_grader()
        worker = _CannedWorker(broker, verdict=_canned_verdict())
        worker.start()
        try:
            result = grader.grade(
                make_trial_spec(), make_trajectory(status=TrialStatus.COMPLETED), "sys"
            )
        finally:
            worker.stop_worker()
            worker.join(timeout=2)
        assert isinstance(result, Grade)
        assert result.score == pytest.approx(0.8)


class TestWirePayloadOnJob:
    """The producer packs every v2 wire field into the ``GradeJob`` before
    publishing. A drift in the packing step would silently under-populate
    the payload each worker forwards to :meth:`GrpcGraderClient.grade`."""

    def test_grade_packs_every_v2_wire_field_from_spec(self) -> None:
        from tolokaforge.grader.queue import BrokerClosed, GradeJob

        broker = InMemoryGradeBroker()
        grader, _ = _make_grader(broker=broker)
        spec = make_trial_spec()

        captured: list[GradeJob] = []
        completion = threading.Event()

        def _observer() -> None:
            try:
                job = broker.next_job(timeout=2)
            except BrokerClosed:
                return
            assert job is not None
            captured.append(job)
            broker.publish_result(GradeResult(job_id=job.job_id, grade=_canned_verdict(), error=""))
            completion.set()

        worker = threading.Thread(target=_observer, daemon=True)
        worker.start()
        try:
            grader.grade(
                spec,
                make_trajectory(status=TrialStatus.COMPLETED),
                "You are the agent.",
            )
        finally:
            completion.wait(timeout=2)
            worker.join(timeout=2)

        assert len(captured) == 1
        job = captured[0]
        assert job.trial_id == spec.trial_id
        assert job.task_config_json == spec.task.grading.model_dump_json()
        assert job.judge_model_config_json == ""
        assert job.task_description_json == spec.task.model_dump_json()
        assert job.runner_substrate_address == "runner:50051"
        assert job.agent_system_prompt == "You are the agent."


class TestAutoFailBranches:
    def test_error_status_short_circuits_without_publishing(self) -> None:
        grader, broker = _make_grader()
        # No worker running — if the grader published, .result() would time out.
        result = grader.grade(make_trial_spec(), make_trajectory(status=TrialStatus.ERROR), "sys")
        assert result is not None
        assert result.binary_pass is False

    def test_stuck_termination_short_circuits(self) -> None:
        grader, _ = _make_grader()
        traj = make_trajectory(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.STUCK_DETECTED
        )
        result = grader.grade(make_trial_spec(), traj, "sys")
        assert result is not None
        assert result.binary_pass is False


class TestFailureIsLoud:
    def test_worker_error_raises_grading_failed_error(self) -> None:
        grader, broker = _make_grader()
        worker = _CannedWorker(
            broker,
            verdict=None,
            error_by_trial={make_trial_spec().trial_id: "worker blew up"},
        )
        worker.start()
        try:
            with pytest.raises(GradingFailedError, match="worker blew up"):
                grader.grade(
                    make_trial_spec(),
                    make_trajectory(status=TrialStatus.COMPLETED),
                    "sys",
                )
        finally:
            worker.stop_worker()
            worker.join(timeout=2)


class TestThroughputProperty:
    """Locks ADR-0038's Decision 3: independent throughput scale.

    With N jobs each taking ``d`` seconds and ``k`` consumers, wall-clock
    is roughly ``N / k * d`` — not ``N * d``. The test uses a coarse
    threshold rather than an exact bound because CI machines vary.
    """

    def test_two_consumers_finish_faster_than_the_serial_wall_clock(self) -> None:
        broker = InMemoryGradeBroker()
        per_job = 0.05
        n_jobs = 8

        workers = [
            _CannedWorker(broker, verdict=_canned_verdict(), per_job_delay=per_job)
            for _ in range(2)
        ]
        for w in workers:
            w.start()

        producers = [
            threading.Thread(
                target=lambda i=i: _make_grader(broker=broker, timeout_s=5)[0].grade(
                    make_trial_spec(trial_id=f"task:{i}"),
                    make_trajectory(status=TrialStatus.COMPLETED),
                    "sys",
                )
            )
            for i in range(n_jobs)
        ]

        start = time.monotonic()
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=5)
        elapsed = time.monotonic() - start

        for w in workers:
            w.stop_worker()
        for w in workers:
            w.join(timeout=2)

        serial_bound = n_jobs * per_job  # 0.4s
        # Two consumers should complete meaningfully under the serial bound.
        assert elapsed < serial_bound * 0.9, (
            f"Queue variant did not amortise: elapsed {elapsed:.3f}s vs serial "
            f"bound {serial_bound:.3f}s. Consumers may not be pulling in parallel."
        )


class TestFactoryAndRegistration:
    def test_factory_builds_broker_and_worker_pool(self) -> None:
        """The registered ``queue`` factory returns a working grader:
        an in-memory broker, N daemon worker threads holding grader-service
        clients, and a :class:`QueueTrialGrader` that owns both. ``close``
        tears everything down.
        """
        from tolokaforge.core.models.run_config import GraderConfig, QueueGraderConfig

        ctx = TrialGraderContext(
            runner_address="stub:0",
            logger=MagicMock(),
            grader_config=GraderConfig(queue=QueueGraderConfig(workers=2)),
        )
        grader = queue_trial_grader_factory(ctx)
        # Snapshot the worker handles BEFORE close(); the impl clears the
        # list as part of shutdown, so a post-close ``for w in grader._workers``
        # would iterate zero times and assert nothing.
        workers = list(grader._workers)
        try:
            assert isinstance(grader, QueueTrialGrader)
            assert len(workers) == 2
            assert grader._owns_broker is True
        finally:
            grader.close()

        for worker in workers:
            assert not worker.is_alive(), "close() must drain the worker pool"
        assert grader._workers == [], "close() clears the worker list"

    def test_factory_threads_runner_address_to_runner_substrate_address(self) -> None:
        """``SubstrateService`` shares the runner's listen port; each
        worker's ``GradeJob`` carries ``ctx.runner_address`` so the
        grader-side composite dispatcher can dial it per trial."""
        from tolokaforge.core.models.run_config import GraderConfig, QueueGraderConfig

        ctx = TrialGraderContext(
            runner_address="runner.grid-01:50051",
            grader_address="grader.grid-02:50052",
            logger=MagicMock(),
            grader_config=GraderConfig(queue=QueueGraderConfig(workers=1)),
        )
        grader = queue_trial_grader_factory(ctx)
        try:
            assert grader.runner_substrate_address == "runner.grid-01:50051"
        finally:
            grader.close()

    def test_factory_rejects_missing_address(self) -> None:
        """The queue transport needs a downstream ``grader_rpc`` target;
        neither ``grader_address`` nor ``runner_address`` set means the
        factory refuses at startup instead of publishing jobs no worker
        can grade."""
        ctx = TrialGraderContext(runner_address=None, logger=MagicMock())
        with pytest.raises(ValueError, match="grader_address"):
            queue_trial_grader_factory(ctx)

    def test_factory_rejects_unsupported_worker_grader(self) -> None:
        """Only ``worker_grader: grader_rpc`` is wired today; other names
        raise at factory time so the operator sees the gap before jobs
        pile up."""
        from tolokaforge.core.models.run_config import GraderConfig, QueueGraderConfig

        cfg = GraderConfig(queue=QueueGraderConfig(worker_grader="judge_only"))
        ctx = TrialGraderContext(runner_address="stub:0", logger=MagicMock(), grader_config=cfg)
        with pytest.raises(ValueError, match="worker_grader"):
            queue_trial_grader_factory(ctx)

    def test_registered_under_queue_entry_point(self) -> None:
        factory = load_trial_grader("queue")
        assert factory is queue_trial_grader_factory
