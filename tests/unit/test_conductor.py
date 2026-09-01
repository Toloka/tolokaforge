"""Unit tests for ``tolokaforge.core.conductor``.

Covers the in-memory conductor's own shape, the call log dataclass,
the default trajectory factory, and the in-process conductor's split of the
register response into the agent's and the user actor's tool surfaces.
Cross-implementation parity lives in ``tests/canonical/test_conductor_contract.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tolokaforge.core.conductor import (
    DEFAULT_MAX_TURNS,
    ConductorCallLog,
    InMemoryConductor,
    InProcessConductor,
    _default_success_trajectory,
    _TrialSetup,
    resolve_max_turns,
)
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    EvaluationConfig,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    ResetSpec,
    RunConfig,
    ServiceSpec,
    TaskConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
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

    def test_skips_get_state_rpc_when_task_declares_no_json_db(self) -> None:
        """No ``initial_state.json_db`` → ``RegisterTrial`` never provisioned
        the DB Service (:func:`~tolokaforge.runner.models.provisions_database`),
        so ``GetState`` has no target and the RPC is skipped."""
        spec = _make_spec()
        setup = self._setup()
        setup.env_state.config.json_db = None
        conductor = self._conductor()

        conductor._capture_final_state(spec, setup, _default_success_trajectory("t1", 0))

        conductor.runtime_backend.get_state.assert_not_called()


class TestResolveMaxTurns:
    def test_orchestrator_default_is_50(self) -> None:
        # Pre-M9 semantic: run-level cap is always-on, defaults to 50.
        # A future release will flip this to opt-in (default None).
        assert OrchestratorConfig().max_turns == 50

    def test_default_config_clamps_task_value_to_run_cap(self) -> None:
        # A task authoring max_turns=100 clamps to the run cap default (50).
        # To let a higher task value stand, an operator must raise the run cap.
        assert resolve_max_turns(100, OrchestratorConfig().max_turns) == 50

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


# ---------------------------------------------------------------------------
# The trial's two tool surfaces
# ---------------------------------------------------------------------------


def _wire_schema(name: str) -> dict[str, Any]:
    """One entry as ``register_trial`` returns it, pre-sanitisation."""
    return {
        "name": name,
        "description": f"tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }


def _register_result(agent: list[str], user: list[str]) -> dict[str, Any]:
    """The register response's shape: one concatenated list, agent slice first,
    partitioned at ``num_agent_tools`` (``runner.proto``)."""
    return {
        "success": True,
        "error": None,
        "tool_schemas": [_wire_schema(n) for n in agent + user],
        "num_agent_tools": len(agent),
        "num_user_tools": len(user),
    }


@dataclass
class _RunnerStub:
    """The three ``TrialRunner`` attributes ``_write_artifacts`` reads."""

    effective_system_prompt: str
    user_system_prompt: str
    logger: StructuredLogger


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


class TestTrialToolSurfacePartition:
    """``RegisterTrialResponse`` carries both actors' tools in one list. The
    conductor slices it at ``num_agent_tools`` and offers each actor only its
    own half — the runner's registries are split the same way, so a tool
    offered to the wrong actor is refused ``TOOL_NOT_FOUND`` when called.
    """

    def _conductor(self, tmp_path: Path, register_result: dict[str, Any]) -> InProcessConductor:
        adapter = MagicMock()
        adapter.get_task_dir.return_value = tmp_path / "task"
        adapter.create_environment.return_value = MagicMock(data={})
        adapter.get_grading_config.return_value = None

        runtime_backend = MagicMock()
        runtime_backend.register_trial.return_value = register_result

        agent_client = MagicMock()
        agent_client.config = ModelConfig(provider="openai", name="gpt-4")
        agent_client.capabilities.schema_sanitizer.sanitize.side_effect = lambda s: s

        return InProcessConductor(
            adapter=adapter,
            artifact_writer=FileArtifactWriter(),
            config=RunConfig(
                models={"agent": ModelConfig(provider="openai", name="gpt-4")},
                orchestrator=OrchestratorConfig(auto_start_services=False),
                evaluation=EvaluationConfig(output_dir=str(tmp_path)),
            ),
            logger=StructuredLogger("test-tool-surface"),
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            trial_grader=MagicMock(),
            output_dir=tmp_path,
        )

    def _setup(self, tmp_path: Path, agent: list[str], user: list[str]) -> _TrialSetup:
        conductor = self._conductor(tmp_path, _register_result(agent, user))
        return conductor._setup_trial(_make_spec(), TaskConfig(task_id="t1", description="d"))

    def test_setup_partitions_the_register_response_at_num_agent_tools(
        self, tmp_path: Path
    ) -> None:
        setup = self._setup(tmp_path, agent=["agent_read", "agent_write"], user=["user_probe"])

        assert _names(setup.tool_schemas) == ["agent_read", "agent_write"]
        assert _names(setup.user_tool_schemas) == ["user_probe"]

    def test_the_user_side_executor_is_built_under_the_user_identity(self, tmp_path: Path) -> None:
        setup = self._setup(tmp_path, agent=["agent_read"], user=["user_probe"])

        assert setup.tool_executor.executor == "agent"
        assert setup.user_tool_executor is not None
        assert setup.user_tool_executor.executor == "user"
        assert setup.user_tool_executor.trial_id == setup.tool_executor.trial_id

    def test_a_task_declaring_no_user_tools_builds_no_user_side_executor(
        self, tmp_path: Path
    ) -> None:
        """Every pack in the tree is this one: it must construct exactly what it
        constructed before the user actor could hold tools at all."""
        setup = self._setup(tmp_path, agent=["agent_read"], user=[])

        assert _names(setup.tool_schemas) == ["agent_read"]
        assert setup.user_tool_schemas == []
        assert setup.user_tool_executor is None

    def test_the_agent_and_the_simulator_are_offered_disjoint_surfaces(
        self, tmp_path: Path
    ) -> None:
        """Neither list leaks into the other: the agent is offered the agent
        slice and the simulator the user slice, and no tool appears in both."""
        conductor = self._conductor(tmp_path, _register_result([], []))
        setup = _TrialSetup(
            trial_id="t1:0",
            trial_idx=0,
            task_dir=tmp_path,
            trial_dir=tmp_path / "trials" / "t1" / "0",
            env_state=MagicMock(),
            adapter_env=MagicMock(),
            tool_schemas=[{"type": "function", "function": _wire_schema("agent_read")}],
            tool_executor=MagicMock(),
            user_tool_schemas=[{"type": "function", "function": _wire_schema("user_probe")}],
            user_tool_executor=MagicMock(),
        )

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(
                _make_spec(), TaskConfig(task_id="t1", description="d"), setup
            )

        kwargs = runner_cls.call_args.kwargs
        assert _names(kwargs["tool_schemas"]) == ["agent_read"]
        assert _names(kwargs["user_simulator"].tool_schemas) == ["user_probe"]
        assert kwargs["user_tool_executor"] is setup.user_tool_executor

    def test_a_simulator_with_no_user_tools_is_offered_none(self, tmp_path: Path) -> None:
        """The negative half of the disjointness lock: an empty user slice must
        reach the simulator as ``None``, not as the agent's list."""
        conductor = self._conductor(tmp_path, _register_result([], []))
        setup = _TrialSetup(
            trial_id="t1:0",
            trial_idx=0,
            task_dir=tmp_path,
            trial_dir=tmp_path / "trials" / "t1" / "0",
            env_state=MagicMock(),
            adapter_env=MagicMock(),
            tool_schemas=[{"type": "function", "function": _wire_schema("agent_read")}],
            tool_executor=MagicMock(),
            user_tool_schemas=[],
            user_tool_executor=None,
        )

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(
                _make_spec(), TaskConfig(task_id="t1", description="d"), setup
            )

        kwargs = runner_cls.call_args.kwargs
        assert kwargs["user_simulator"].tool_schemas == []
        assert kwargs["user_tool_executor"] is None

    def test_the_bundle_records_both_slices_in_order(self, tmp_path: Path) -> None:
        """``tools_schemas.yaml`` is the trial's whole declared tool surface.
        ``grading.trace_replay.tool_inventory_from_bundle`` rebuilds the replay
        authoring gate's ``ToolInventory`` from this one file, so an agent-only
        record would make a matcher naming a user tool unblessable.
        """
        conductor = self._conductor(tmp_path, _register_result([], []))
        trial_dir = tmp_path / "trials" / "t1" / "0"
        setup = _TrialSetup(
            trial_id="t1:0",
            trial_idx=0,
            task_dir=tmp_path,
            trial_dir=trial_dir,
            env_state=MagicMock(),
            adapter_env=MagicMock(),
            tool_schemas=[{"type": "function", "function": _wire_schema("agent_read")}],
            tool_executor=MagicMock(),
            user_tool_schemas=[{"type": "function", "function": _wire_schema("user_probe")}],
            user_tool_executor=MagicMock(),
        )
        trajectory = Trajectory(
            task_id="t1",
            trial_index=0,
            start_ts=datetime.now(UTC),
            end_ts=datetime.now(UTC),
            status=TrialStatus.COMPLETED,
            messages=[],
            metrics=Metrics(),
        )

        conductor._write_artifacts(
            _make_spec(),
            TaskConfig(task_id="t1", description="d"),
            setup,
            trajectory,
            _RunnerStub("agent sys", "user sys", StructuredLogger("test-bundle")),
        )

        written = yaml.safe_load((trial_dir / "tools_schemas.yaml").read_text())
        assert _names(written) == ["agent_read", "user_probe"]
