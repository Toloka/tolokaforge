"""``InProcessConductor._setup_trial`` sync gate — locks capability-flag dispatch.

Locks the invariant that :class:`~tolokaforge.core.conductor.InProcessConductor`
publishes an adapter's :class:`~tolokaforge.adapters.base.AdapterEnvironment`
data into the runner's ``TrialState`` iff the adapter opts in via the
``syncs_adapter_env_to_state`` capability flag *and* the data is truthy.
Three cases:

* flag ``True`` + non-empty data → data reaches ``env_state.db_state``.
* flag ``False`` + non-empty data → data is dropped; ``env_state.db_state``
  keeps its pre-sync value.
* flag ``True`` + empty data → the ``adapter_env.data and …`` short-circuit
  skips the sync.

Runs the production ``_setup_trial`` phase end-to-end against real
:class:`~tolokaforge.adapters.base.BaseAdapter` subclasses (not
:class:`MagicMock` — the flag would fail-open on any unspecced mock).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    EvaluationConfig,
    GradingConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.canonical


class _StubSyncingAdapter(BaseAdapter):
    """Opts into ``syncs_adapter_env_to_state``; returns caller-supplied data."""

    syncs_adapter_env_to_state: ClassVar[bool] = True

    def __init__(self, params: dict[str, Any], task_dir: Path, env_data: dict[str, Any]) -> None:
        super().__init__(params)
        self._task_dir = task_dir
        self._env_data = env_data

    def get_task_ids(self) -> list[str]:
        return ["t1"]

    def get_task(self, task_id: str) -> TaskConfig:
        return TaskConfig(task_id=task_id, description="stub")

    def get_task_dir(self, task_id: str) -> Path:
        return self._task_dir

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        return AdapterEnvironment(
            data=self._env_data,
            tools=[],
            wiki="",
            rules=[],
            task_dir=self._task_dir,
        )

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    def get_system_prompt(self, task_id: str) -> str:
        return ""

    def get_grading_config(self, task_id: str) -> GradingConfig:
        return GradingConfig()

    def reset_environment(self, env: AdapterEnvironment) -> None:
        return None

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        return None

    def to_task_description(self, task_id: str) -> TaskDescription:
        return TaskDescription(
            task_id=task_id,
            name=task_id,
            category="stub",
            description="stub",
            adapter_type="native",
            system_prompt="",
        )


class _StubNonSyncingAdapter(_StubSyncingAdapter):
    """Inherits the sync stub, but leaves ``syncs_adapter_env_to_state`` at the ``False`` default."""

    syncs_adapter_env_to_state: ClassVar[bool] = False


def _make_spec(task_id: str = "t1") -> TrialSpec:
    return TrialSpec(
        trial_id=f"{task_id}:0",
        run_id="test-run",
        attempt_id=0,
        worker_id=None,
        task=TaskDescription(
            task_id=task_id,
            name=task_id,
            category="stub",
            description="stub",
            adapter_type="native",
            system_prompt="",
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        max_turns=10,
        default_tool_timeout_s=30.0,
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


def _register_result() -> dict[str, Any]:
    return {
        "success": True,
        "error": None,
        "tool_schemas": [],
        "num_agent_tools": 0,
        "num_user_tools": 0,
    }


def _build_conductor(tmp_path: Path, adapter: BaseAdapter) -> InProcessConductor:
    runtime_backend = MagicMock()
    runtime_backend.register_trial.return_value = _register_result()

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
        logger=StructuredLogger("test-conductor-adapter-env-sync-gate"),
        agent_client=agent_client,
        runtime_backend=runtime_backend,
        trial_grader=MagicMock(),
        output_dir=tmp_path,
    )


def test_adapter_with_syncs_flag_true_and_data_publishes_to_env_state(tmp_path: Path) -> None:
    """flag=True + non-empty data → the sync writes ``adapter_env.data`` into ``env_state.db_state``."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    payload = {"users": [{"id": 1}]}
    adapter = _StubSyncingAdapter({}, task_dir=task_dir, env_data=payload)
    conductor = _build_conductor(tmp_path, adapter)

    setup = conductor._setup_trial(_make_spec(), TaskConfig(task_id="t1", description="stub"))

    assert setup.env_state.db_state == payload


def test_adapter_with_syncs_flag_false_and_data_does_not_publish(tmp_path: Path) -> None:
    """flag=False + non-empty data → the sync is skipped; ``env_state.db_state`` keeps its pre-sync value."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    adapter = _StubNonSyncingAdapter({}, task_dir=task_dir, env_data={"users": [{"id": 2}]})
    conductor = _build_conductor(tmp_path, adapter)

    setup = conductor._setup_trial(_make_spec(), TaskConfig(task_id="t1", description="stub"))

    assert setup.env_state.db_state == {}


def test_adapter_with_syncs_flag_true_and_empty_data_does_not_publish(tmp_path: Path) -> None:
    """flag=True + empty data → the ``adapter_env.data and …`` short-circuit skips the sync."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    adapter = _StubSyncingAdapter({}, task_dir=task_dir, env_data={})
    conductor = _build_conductor(tmp_path, adapter)

    setup = conductor._setup_trial(_make_spec(), TaskConfig(task_id="t1", description="stub"))

    assert setup.env_state.db_state == {}
