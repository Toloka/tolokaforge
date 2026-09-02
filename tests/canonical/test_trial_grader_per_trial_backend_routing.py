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
correct per-trial runner client. The P2/P3 (out-of-process) dispatch shape
is locked at unit level by
``tests/unit/test_trial_grader.py::TestRuntimeBackendDispatchPreference::
test_runner_client_used_when_runtime_backend_is_none``, and factory→client
ownership is locked at canonical level by
``TestRunnerRpcTrialGraderFactory::test_factory_returns_grader_with_owned_
runner_client`` in ``test_trial_grader_context_hygiene.py`` — no duplicate
canonical test for the P2/P3 path lives here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.composition_runtime import ComposedEnvHandle
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
        # Inject a per-trial handle carrying the fake client, and mark
        # the trial as already-connected — bypasses docker-compose
        # provisioning + the gRPC health-check. The test is a wire-shape
        # lock, not a real per-trial substrate exerciser.
        env_handle = ComposedEnvHandle(
            trial_id=trial_id,
            trial_stack_handles=(),
            trial_endpoints=None,
            trial_runner_client=fake_client,  # type: ignore[arg-type]
        )
        backend._delegate._env_handles[trial_id] = env_handle
        backend._delegate._connected_trials.add(trial_id)

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


class TestBuildConductorGatesRuntimeBackendOnAddress:
    """``orchestrator._build_conductor`` populates ``TrialGraderContext.
    runtime_backend`` ONLY when the backend has no static ``runner_address``.
    For shared-stack backends (which have one), the shim stays ``None`` so
    ADR-0038's "grader owns its own client, independent of the orchestrator"
    invariant is preserved for every case that can honour it. Only per-trial
    backends (each trial owns its own endpoint) get the shim.
    """

    def _captured_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        backend_runner_address: str | None,
    ) -> TrialGraderContext:
        """Build a conductor with a backend whose ``runner_address`` is
        ``backend_runner_address`` (``None`` means the attribute is
        missing entirely, mirroring ``PerTrialRuntimeBackend``); return
        the ``TrialGraderContext`` the orchestrator passed to
        ``load_trial_grader``. Real ``_build_conductor``; real
        ``runner_rpc_trial_grader_factory`` swapped for a capturing shim."""
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

        captured: dict[str, TrialGraderContext] = {}

        def _capturing_factory(ctx: TrialGraderContext) -> Any:
            captured["ctx"] = ctx

            class _Stub:
                def grade(self, *a: Any, **kw: Any) -> None:
                    return None

            return _Stub()

        monkeypatch.setattr(
            "tolokaforge.core.orchestrator.load_trial_grader",
            lambda name: _capturing_factory,
        )

        def _conductor_factory(_ctx: ConductorContext) -> InMemoryConductor:
            return InMemoryConductor()

        # Build a lightweight runtime_backend whose ``runner_address``
        # attribute exists (shared-stack shape) or is absent (per-trial
        # shape) — the orchestrator's ``getattr(..., None)`` reads it.
        if backend_runner_address is None:
            # No attribute at all — MagicMock synthesizes attrs by default,
            # so use ``spec=[]`` to force ``getattr`` fallback to ``None``.
            backend = MagicMock(spec=[])
        else:
            backend = MagicMock()
            backend.runner_address = backend_runner_address

        from tolokaforge.core.models.run_config import (
            EvaluationConfig,
            ModelConfig,
            OrchestratorConfig,
            RunConfig,
        )

        cfg = RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
        )
        orch = Orchestrator(cfg, deps=OrchestratorDeps(conductor_factory=_conductor_factory))
        orch.adapter = MagicMock()
        orch.adapter.trial_grader_name = "runner_rpc"

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=backend,
            output_dir=tmp_path,
            request_limiter=None,
        )
        return captured["ctx"]

    def test_per_trial_backend_gets_the_shim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """No ``runner_address`` on the backend (``PerTrialRuntimeBackend``
        shape) → the shim is populated so the grader can route per-trial."""
        ctx = self._captured_context(monkeypatch, tmp_path, backend_runner_address=None)
        assert ctx.runner_address is None
        assert ctx.runtime_backend is not None

    def test_shared_stack_backend_does_not_get_the_shim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A backend with ``runner_address`` set (shared-stack shape) → the
        shim stays ``None``; the grader owns its own client bound to the
        address, preserving ADR-0038's independence invariant for every
        case that can honour it. Regression lock for the reviewer's Major
        finding on PR #1328."""
        ctx = self._captured_context(monkeypatch, tmp_path, backend_runner_address="runner:50051")
        assert ctx.runner_address == "runner:50051"
        assert ctx.runtime_backend is None
