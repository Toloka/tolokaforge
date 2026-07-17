"""Unit tests for ``tolokaforge.core.conductor``.

Covers the in-memory conductor's own shape, the call log dataclass,
and the default trajectory factory. Cross-implementation parity lives
in ``tests/canonical/test_conductor_contract.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import (
    DEFAULT_MAX_TURNS,
    ConductorCallLog,
    InMemoryConductor,
    InProcessConductor,
    _default_success_trajectory,
    resolve_max_turns,
)
from tolokaforge.core.models import (
    ModelConfig,
    OrchestratorConfig,
    ResetSpec,
    ServiceSpec,
)
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


def _make_spec(
    *,
    task_id: str = "t1",
    trial_idx: int = 0,
    attempt_id: int = 0,
    worker_id: str | None = None,
) -> TrialSpec:
    return TrialSpec(
        trial_id=f"{task_id}:{trial_idx}",
        run_id="test-run",
        attempt_id=attempt_id,
        worker_id=worker_id,
        task=TaskDescription(
            task_id=task_id,
            name=task_id,
            category="test",
            description="unit-test stub",
            adapter_type="native",
            system_prompt="",
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        max_turns=10,
        default_tool_timeout_s=30.0,
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


class TestConductorCallLog:
    def test_default_fields_are_empty(self) -> None:
        log = ConductorCallLog()
        assert log.runs == []

    def test_equality_holds_for_identical_state(self) -> None:
        assert ConductorCallLog() == ConductorCallLog()

    def test_inequality_when_runs_diverge(self) -> None:
        a = ConductorCallLog()
        b = ConductorCallLog()
        a.runs.append({"trial_id": "x:0"})
        assert a != b


class TestDefaultSuccessTrajectoryFactory:
    """The default factory builds a minimal completed trajectory with a
    passing grade. Sufficient for tests that don't care about content;
    callers that need failure scenarios pass a custom factory.
    """

    def test_default_trajectory_is_completed(self) -> None:
        from tolokaforge.core.models import TrialStatus

        traj = _default_success_trajectory("airline_001", 0)
        assert traj.status == TrialStatus.COMPLETED

    def test_default_trajectory_grade_is_passing(self) -> None:
        traj = _default_success_trajectory("airline_001", 0)
        assert traj.grade is not None
        assert traj.grade.binary_pass is True
        assert traj.grade.score == 1.0

    def test_default_trajectory_carries_task_and_index(self) -> None:
        traj = _default_success_trajectory("airline_001", 3)
        assert traj.task_id == "airline_001"
        assert traj.trial_index == 3

    def test_default_trajectory_has_empty_message_history(self) -> None:
        traj = _default_success_trajectory("airline_001", 0)
        assert traj.messages == []


class TestInMemoryConductorConstruction:
    def test_fresh_backend_has_a_call_log(self) -> None:
        backend = InMemoryConductor()
        assert isinstance(backend.call_log, ConductorCallLog)

    def test_fresh_backend_call_log_is_empty(self) -> None:
        backend = InMemoryConductor()
        assert backend.call_log.runs == []

    def test_each_backend_has_independent_call_log(self) -> None:
        a = InMemoryConductor()
        b = InMemoryConductor()
        a.run(_make_spec(), MagicMock(task_id="t1"))
        assert len(a.call_log.runs) == 1
        assert b.call_log.runs == []

    def test_custom_factory_replaces_default(self) -> None:
        from datetime import UTC, datetime

        from tolokaforge.core.models import Metrics, Trajectory, TrialStatus

        seen_args: list[tuple[str, int]] = []

        def factory(task_id: str, trial_idx: int):
            seen_args.append((task_id, trial_idx))
            now = datetime.now(UTC)
            return Trajectory(
                task_id=task_id,
                trial_index=trial_idx,
                start_ts=now,
                end_ts=now,
                status=TrialStatus.FAILED,
                messages=[],
                metrics=Metrics(),
                grade=None,
            )

        backend = InMemoryConductor(trajectory_factory=factory)
        backend.run(_make_spec(trial_idx=5), MagicMock(task_id="t1"))
        assert seen_args == [("t1", 5)]


class TestInMemoryConductorRun:
    def test_trial_id_format_is_canonical(self) -> None:
        backend = InMemoryConductor()
        result = backend.run(
            _make_spec(task_id="airline_001", trial_idx=3),
            MagicMock(task_id="airline_001"),
        )
        assert result.trial_id == "airline_001:3"

    def test_worker_id_threads_through(self) -> None:
        backend = InMemoryConductor()
        result = backend.run(
            _make_spec(worker_id="worker-42"),
            MagicMock(task_id="t1"),
        )
        assert result.worker_id == "worker-42"

    def test_worker_id_default_is_none(self) -> None:
        backend = InMemoryConductor()
        result = backend.run(_make_spec(), MagicMock(task_id="t1"))
        assert result.worker_id is None


class TestTrialSpecWireExclusion:
    """``environment_manifest`` describes the orchestrator's substrate
    provisioning intent; the runner runs inside that substrate and never
    consumes the manifest. The register_trial RPC therefore serialises
    ``TrialSpec`` with ``exclude={"task": {"environment_manifest"}}`` so
    the runner-side ``TaskDescription`` validator doesn't attempt to
    re-validate a ``compose_file`` path resolved on the orchestrator's
    local filesystem.
    """

    def test_environment_manifest_is_excluded_from_wire_dump(self) -> None:
        fixture = (
            Path(__file__).parent.parent
            / "canonical"
            / "fixtures"
            / "environment_manifest"
            / "safe_one_service.yaml"
        )
        spec = _make_spec()
        spec = spec.model_copy(
            update={
                "task": spec.task.model_copy(
                    update={"environment_manifest": EnvironmentManifest(compose_file=fixture)}
                )
            }
        )

        wire_json = spec.model_dump_json(exclude={"task": {"environment_manifest"}})

        assert "environment_manifest" not in wire_json
        assert "compose_file" not in wire_json

    def test_other_task_fields_survive_exclusion(self) -> None:
        """The exclusion is scoped to ``environment_manifest`` alone —
        the rest of ``TaskDescription`` still crosses the wire so the
        runner can identify the trial's task."""
        fixture = (
            Path(__file__).parent.parent
            / "canonical"
            / "fixtures"
            / "environment_manifest"
            / "safe_one_service.yaml"
        )
        spec = _make_spec(task_id="t1")
        spec = spec.model_copy(
            update={
                "task": spec.task.model_copy(
                    update={"environment_manifest": EnvironmentManifest(compose_file=fixture)}
                )
            }
        )

        wire_json = spec.model_dump_json(exclude={"task": {"environment_manifest"}})

        assert '"task_id":"t1"' in wire_json
        assert '"adapter_type":"native"' in wire_json


class TestCaptureFinalStateEnvironmentBlock:
    """``_capture_final_state`` records the resolved environment identity under
    ``final_env_state["environment"]`` only when the trial's task carries an
    ``environment_manifest``. Manifest-less trials keep the JSON-DB-only shape —
    the additive/back-compat guarantee the descriptor rests on.
    """

    def _conductor(self) -> InProcessConductor:
        conductor = InProcessConductor(
            adapter=MagicMock(),
            artifact_writer=MagicMock(),
            config=MagicMock(),
            logger=MagicMock(),
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=Path("/tmp"),
        )
        conductor.runtime_backend.get_state.return_value = {"success": False}
        return conductor

    def _setup(self) -> MagicMock:
        setup = MagicMock()
        setup.trial_id = "t1:0"
        setup.env_state.get_final_state.return_value = {}
        setup.env_state.agent_visible_dir = Path("/tmp/agent")
        setup.adapter_env.data = None
        return setup

    def _manifest(self) -> EnvironmentManifest:
        fixture = (
            Path(__file__).parent.parent
            / "canonical"
            / "fixtures"
            / "environment_manifest"
            / "identity_multi_service.yaml"
        )
        return EnvironmentManifest(
            compose_file=fixture,
            runner_service="runner",
            services={
                "runner": ServiceSpec(isolation="shared"),
                "app-service": ServiceSpec(isolation="shared"),
                "app-db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            },
        )

    def test_manifest_bearing_trial_gains_environment_block(self) -> None:
        spec = _make_spec()
        spec = spec.model_copy(
            update={"task": spec.task.model_copy(update={"environment_manifest": self._manifest()})}
        )
        trajectory = _default_success_trajectory("t1", 0)

        self._conductor()._capture_final_state(spec, self._setup(), trajectory)

        environment = trajectory.final_env_state["environment"]
        assert set(environment["services"]) == {"runner", "app-service", "app-db"}
        assert environment["services"]["app-service"]["dsns"] == [
            "postgresql://app:***@app-db:5432/mfg"
        ]
        assert "app_pw" not in json.dumps(trajectory.final_env_state)

    def test_manifest_less_trial_omits_environment_block(self) -> None:
        spec = _make_spec()
        assert spec.task.environment_manifest is None
        trajectory = _default_success_trajectory("t1", 0)

        self._conductor()._capture_final_state(spec, self._setup(), trajectory)

        assert "environment" not in trajectory.final_env_state


class TestResolveMaxTurns:
    def test_orchestrator_default_is_unset(self) -> None:
        assert OrchestratorConfig().max_turns is None

    def test_default_config_leaves_task_value_uncapped(self) -> None:
        assert resolve_max_turns(100, OrchestratorConfig().max_turns) == 100

    def test_both_unset_falls_back_to_engine_default(self) -> None:
        assert resolve_max_turns(None, None) == DEFAULT_MAX_TURNS == 50

    def test_run_cap_clamps_higher_task_value(self) -> None:
        assert resolve_max_turns(100, 30) == 30

    def test_task_value_stands_when_run_cap_unset(self) -> None:
        assert resolve_max_turns(30, None) == 30

    def test_run_cap_stands_when_task_value_unset(self) -> None:
        assert resolve_max_turns(None, 30) == 30

    def test_tighter_of_two_set_values_wins(self) -> None:
        assert resolve_max_turns(100, 200) == 100
