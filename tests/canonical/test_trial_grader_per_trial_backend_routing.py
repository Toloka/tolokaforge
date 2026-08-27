"""Regression lock: ``PerTrialRuntimeBackend`` + ``RunnerRPCTrialGrader``
must produce a real :class:`Grade`, not a phantom-address gRPC error.

Before this fix, ``orchestrator._build_conductor`` did
``runner_address = getattr(runtime_backend, "runner_address", None)`` and
passed the resulting ``None`` into ``TrialGraderContext``. The
``runner_rpc`` factory then built a ``GrpcRunnerClient(runner_address="")``.
On the first ``grade_trial`` call, that client's connect health-check spun
for ~30 s and raised, so ``RunnerRPCTrialGrader.grade`` raised
:class:`GradingFailedError`, ``binary_pass`` stayed ``None`` for every
completed trial, and no ``grade.yaml`` was ever written. On the
``eval/tbench-balanced-10-engine-loop`` sample sweep this shape produced
110/110 ungraded trials on $50 of real LLM spend.

This test pins the fix — ``ctx.runtime_backend`` is threaded through the
context and the grader delegates to ``backend.grade_trial(trial_id, ...)``,
which ``PerTrialRuntimeBackend._client_for(trial_id)`` routes to the
correct per-trial runner client. The companion test locks that the
out-of-process (P2/P3) shape ADR-0038 pins is not regressed by the change.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.models import Grade, TerminationReason, TrialStatus
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.plugin_registry import TrialGraderContext
from tolokaforge.core.trial_grader import runner_rpc_trial_grader_factory

pytestmark = pytest.mark.canonical


class _FakePerTrialRunnerClient:
    """Stub that satisfies the ``RunnerClient`` Protocol slice
    ``PerTrialRuntimeBackend`` calls into.

    ``connect()`` is a no-op — the real client's connect gates on a runner
    health-check; the test bypasses that by adding ``trial_id`` to
    ``_connected_trials`` before ``grade`` runs. ``health_check()`` returns
    ``True`` so any late invariant probe stays satisfied.
    """

    def __init__(self, grade_result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._grade_result = grade_result or {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {
                    "state_checks": 1.0,
                    "transcript_rules": -1.0,
                    "llm_judge": -1.0,
                    "custom_checks": -1.0,
                },
                "reasons": "ok",
            },
        }

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        # No-op — the test injects the client into an already-connected
        # backend so this path is never exercised by ``grade_trial``.
        return None

    def health_check(self) -> bool:
        return True

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "trial_id": trial_id,
                "llm_messages_json": llm_messages_json,
                "grading_components": grading_components,
                "termination_reason": termination_reason,
            }
        )
        return self._grade_result


class TestPerTrialBackendRoutesThroughRunnerRPCGrader:
    """``RunnerRPCTrialGrader.grade`` must route through
    ``PerTrialRuntimeBackend.grade_trial`` when the context carries the
    backend, so per-trial engine-loop runs produce a Grade instead of an
    empty-address gRPC error."""

    def test_grade_reaches_per_trial_client_and_returns_a_grade(self) -> None:
        """The regression scenario: PerTrialRuntimeBackend + runner_rpc grader
        + a USER_STOP-terminated trajectory MUST produce a Grade. Before the
        fix, this test hangs on the phantom-address health-check retry loop
        (~30 s) and raises ConnectionError — a clean fails-on-main,
        passes-on-fix behaviour lock."""
        spec = make_trial_spec()
        trial_id = spec.trial_id

        fake_client = _FakePerTrialRunnerClient()
        backend = PerTrialRuntimeBackend()
        # Inject a per-trial client and mark it connected, bypassing
        # docker-compose provisioning + the gRPC health-check. The test is
        # a wire-shape lock, not a real per-trial substrate exerciser.
        backend._clients[trial_id] = fake_client  # type: ignore[assignment]
        backend._connected_trials.add(trial_id)

        ctx = TrialGraderContext(
            runner_address=None,
            logger=MagicMock(),
            runtime_backend=backend,
        )
        grader = runner_rpc_trial_grader_factory(ctx)

        # No gRPC client on this dispatch path — the backend is the target.
        assert grader.runtime_backend is backend
        assert grader.runner_client is None

        traj = make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.USER_STOP,
        )
        grade = grader.grade(spec, traj, "sysprompt")

        assert isinstance(grade, Grade)
        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert len(fake_client.calls) == 1
        assert fake_client.calls[0]["trial_id"] == trial_id
        assert fake_client.calls[0]["termination_reason"] == TerminationReason.USER_STOP.value

    def test_p2_p3_path_still_works_without_runtime_backend(self) -> None:
        """The out-of-process shape ADR-0038 pins: no runtime_backend, only
        an address-built client. Stage 2's routing change must not regress
        this path — the grader still dispatches through the client target.

        Monkey-patch ``GrpcRunnerClient`` construction so the factory can
        build without touching the network, then confirm the client the
        factory placed on the grader receives the ``grade_trial`` call."""
        stub_client = _FakePerTrialRunnerClient()

        import tolokaforge.core.shared_stack_runtime as ssr

        original = ssr.GrpcRunnerClient
        # Every ``GrpcRunnerClient(runner_address=...)`` construction from
        # this stub returns the same ``_FakePerTrialRunnerClient``, which
        # implements the RunnerClient Protocol slice the grader touches.
        ssr.GrpcRunnerClient = lambda runner_address: stub_client  # type: ignore[assignment]
        try:
            ctx = TrialGraderContext(
                runner_address="test-runner:9999",
                logger=MagicMock(),
                # No runtime_backend — this is the wire shape.
            )
            grader = runner_rpc_trial_grader_factory(ctx)
        finally:
            ssr.GrpcRunnerClient = original  # type: ignore[assignment]

        assert grader.runtime_backend is None
        assert grader.runner_client is stub_client

        spec = make_trial_spec()
        traj = make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.USER_STOP,
        )
        grade = grader.grade(spec, traj, "sysprompt")

        assert isinstance(grade, Grade)
        assert len(stub_client.calls) == 1
        assert stub_client.calls[0]["trial_id"] == spec.trial_id
