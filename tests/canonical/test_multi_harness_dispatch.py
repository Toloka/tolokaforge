"""Canonical: multi-harness dispatch wiring in the orchestrator (issue #1750, A3).

Mock runtime, no Docker. A two-entry ``harnesses:`` run (a plain adapter
standing in for the native case, plus a registered fake *delegated* adapter)
must, through ``Orchestrator.load_tasks``:

- build a ``CompositeAdapter`` and enumerate both entries' tasks into the
  dispatch matrix (``task_units`` / ``_entry_of_task``),
- route each per-task call (``to_task_description`` via ``_task_description``,
  ``get_task_dir`` via ``_adapter_for_task``) to the owning entry's adapter,
- answer the run-level decisions as explicit unions — adapter fingerprints by
  type, docker-CLI need, docker-stack requirements — reflecting both entries.

The single-adapter path is exercised too, to lock that nothing routes through
the composite when no ``harnesses`` block is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_task_config, make_task_description
from tolokaforge.adapters import DockerStackRequirements, register_adapter
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.adapter_registry import CompositeAdapter
from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.canonical


class _FakeAdapter(BaseAdapter):
    """Adapter that serves a fixed task set; knobs set per subclass."""

    _adapter_type = "fake"
    _ids: list[str] = []

    def get_task_ids(self) -> list[str]:
        return list(self._ids)

    def get_task(self, task_id: str) -> Any:
        return make_task_config(task_id=task_id)

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/fake") / self._adapter_type / task_id

    def to_task_description(self, task_id: str) -> Any:
        return make_task_description(task_id=task_id, adapter_type=self._adapter_type)

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(self, task_id, env) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(self, task_id, env) -> str | None:  # pragma: no cover
        raise NotImplementedError


class _PlainFake(_FakeAdapter):
    """Stands in for the native case: no docker need, no fingerprint."""

    _adapter_type = "fake_a3_plain"
    _ids = ["plain-1", "plain-2"]


class _DelegatedFake(_FakeAdapter):
    _adapter_type = "fake_a3_deleg"
    _ids = ["deleg-1"]
    requires_docker_cli_in_runner = True
    supported_execution_modes = frozenset({ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED})

    def fingerprint(self) -> dict[str, Any] | None:
        return {"kind": "delegated"}

    def docker_stack_requirements(self) -> DockerStackRequirements:
        return DockerStackRequirements(mount_docker_socket=True, needs_rag_service=True)


@pytest.fixture(autouse=True)
def _register_fakes() -> None:
    register_adapter("fake_a3_plain", _PlainFake)
    register_adapter("fake_a3_deleg", _DelegatedFake)


def _multi_harness_config() -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openai", name="gpt-4"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/multi_harness"),
        harnesses={
            "entries": [
                {"name": "plain", "adapter": "fake_a3_plain"},
                {"name": "deleg", "adapter": "fake_a3_deleg"},
            ]
        },
    )


def _single_adapter_config() -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openai", name="gpt-4"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            output_dir="/tmp/single", harness_adapter={"type": "fake_a3_plain"}
        ),
    )


class TestMultiHarnessDispatch:
    def test_load_tasks_builds_composite_and_matrix(self) -> None:
        orch = Orchestrator(_multi_harness_config())
        orch.load_tasks()

        assert isinstance(orch.adapter, CompositeAdapter)
        assert {t.task_id for t in orch.tasks} == {"plain-1", "plain-2", "deleg-1"}
        assert orch._entry_of_task == {
            "plain-1": "plain",
            "plain-2": "plain",
            "deleg-1": "deleg",
        }
        entry_names = {entry_name for entry_name, _adapter, _task in orch.task_units}
        assert entry_names == {"plain", "deleg"}

    def test_per_task_calls_route_to_owning_adapter(self) -> None:
        orch = Orchestrator(_multi_harness_config())
        orch.load_tasks()

        # to_task_description (via _task_description) routes by entry.
        assert orch._task_description("plain-1").adapter_type == "fake_a3_plain"
        assert orch._task_description("deleg-1").adapter_type == "fake_a3_deleg"

        # get_task_dir (via _adapter_for_task) routes by entry.
        assert orch._adapter_for_task("plain-1").get_task_dir("plain-1") == Path(
            "/fake/fake_a3_plain/plain-1"
        )
        assert orch._adapter_for_task("deleg-1").get_task_dir("deleg-1") == Path(
            "/fake/fake_a3_deleg/deleg-1"
        )

    def test_union_run_level_decisions(self) -> None:
        orch = Orchestrator(_multi_harness_config())
        orch.load_tasks()

        # Fingerprints: one per distinct adapter type; plain reports nothing.
        assert orch._adapter_fingerprints() == {"fake_a3_deleg": {"kind": "delegated"}}

        # Docker CLI need is OR'd across entries; the delegated entry needs it.
        assert orch._run_needs_docker_cli_effective() is True

        # Docker-stack requirements are the merged union of both entries.
        assert isinstance(orch.adapter, CompositeAdapter)
        merged = orch.adapter.union_docker_stack_requirements()
        assert merged.mount_docker_socket is True
        assert merged.needs_rag_service is True


class TestSingleAdapterUnchanged:
    def test_single_adapter_path_builds_no_composite(self) -> None:
        orch = Orchestrator(_single_adapter_config())
        orch.load_tasks()

        assert not isinstance(orch.adapter, CompositeAdapter)
        assert orch.task_units == []
        assert orch._entry_of_task == {}
        assert {t.task_id for t in orch.tasks} == {"plain-1", "plain-2"}
        # Single-adapter fingerprints keep the single-type shape (plain → none).
        assert orch._adapter_fingerprints() == {}
        assert orch._run_needs_docker_cli_effective() is False
