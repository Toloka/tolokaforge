"""Unit tests for per-entry adapter resolution + trajectory identity (A4, #1750).

Covers:

- ``TrialSpec.entry`` default and wire round-trip,
- ``Trajectory.harness_entry`` / ``adapter_type`` default, round-trip, and
  backward-compatible deserialisation of OLD bundles that lack the fields,
- the conductor's ``_adapter_for`` resolving the owning adapter per entry
  (so ``create_environment`` / ``get_task_dir`` / ``syncs_adapter_env_to_state``
  / ``get_grading_config`` route to the right adapter), with the single-adapter
  path returning the one adapter unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_description
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.adapter_registry import CompositeAdapter, HarnessEntry
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.models.run_config import HarnessEntryConfig
from tolokaforge.core.models.trajectory import Trajectory
from tolokaforge.core.trial import TrialSpec

pytestmark = pytest.mark.unit


class _EntryFake(BaseAdapter):
    """Adapter whose per-task answers encode its identity."""

    _adapter_type = "fake"
    syncs_adapter_env_to_state = False

    def get_task_ids(self) -> list[str]:
        return []

    def get_task(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/d") / self._adapter_type / task_id

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        return AdapterEnvironment(data={"owner": self._adapter_type}, tools=[], wiki="", rules=[])

    def get_grading_config(self, task_id: str) -> Any:
        return f"grading-{self._adapter_type}"

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(self, task_id, env) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(self, task_id, env) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError


class _AlphaFake(_EntryFake):
    _adapter_type = "alpha"


class _BetaFake(_EntryFake):
    _adapter_type = "beta"
    syncs_adapter_env_to_state = True


def _config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(),
        evaluation=EvaluationConfig(output_dir="/tmp/x"),
    )


def _conductor(adapter: BaseAdapter) -> InProcessConductor:
    return InProcessConductor(
        adapter=adapter,
        artifact_writer=None,  # type: ignore[arg-type]
        config=_config(),
        logger=get_logger("test-conductor"),
        agent_client=None,  # type: ignore[arg-type]
        runtime_backend=None,  # type: ignore[arg-type]
        trial_grader=None,  # type: ignore[arg-type]
        output_dir=Path("/tmp/x"),
    )


def _spec(entry: str = "") -> TrialSpec:
    return TrialSpec(
        trial_id="t:0",
        run_id="run-1",
        entry=entry,
        task=make_task_description(task_id="t"),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        env_endpoints=make_env_endpoints(),
    )


class TestTrialSpecEntry:
    def test_entry_defaults_to_empty(self) -> None:
        assert _spec().entry == ""

    def test_entry_round_trips(self) -> None:
        spec = _spec(entry="alpha")
        restored = TrialSpec.model_validate(spec.model_dump())
        assert restored.entry == "alpha"


class TestTrajectoryIdentity:
    def _trajectory(self, **overrides: Any) -> Trajectory:
        base: dict[str, Any] = {
            "task_id": "t",
            "trial_index": 0,
            "start_ts": datetime.now(tz=timezone.utc),
            "end_ts": datetime.now(tz=timezone.utc),
            "messages": [],
        }
        base.update(overrides)
        return Trajectory(**base)

    def test_identity_fields_default_none(self) -> None:
        traj = self._trajectory()
        assert traj.harness_entry is None
        assert traj.adapter_type is None

    def test_identity_round_trips(self) -> None:
        traj = self._trajectory(harness_entry="beta", adapter_type="tau")
        restored = Trajectory.model_validate(traj.model_dump())
        assert restored.harness_entry == "beta"
        assert restored.adapter_type == "tau"

    def test_old_bundle_without_fields_deserialises(self) -> None:
        payload = self._trajectory().model_dump()
        payload.pop("harness_entry", None)
        payload.pop("adapter_type", None)
        restored = Trajectory.model_validate(payload)
        assert restored.harness_entry is None
        assert restored.adapter_type is None


class TestConductorAdapterResolution:
    def _composite(self) -> CompositeAdapter:
        return CompositeAdapter(
            [
                HarnessEntry(
                    name="alpha",
                    config=HarnessEntryConfig(name="alpha", adapter="alpha"),
                    adapter=_AlphaFake({}),
                    task_ids=["t"],
                ),
                HarnessEntry(
                    name="beta",
                    config=HarnessEntryConfig(name="beta", adapter="beta"),
                    adapter=_BetaFake({}),
                    task_ids=["u"],
                ),
            ]
        )

    def test_resolves_owning_adapter_per_entry(self) -> None:
        conductor = _conductor(self._composite())

        alpha = conductor._adapter_for(_spec(entry="alpha"))
        beta = conductor._adapter_for(_spec(entry="beta"))

        assert alpha.get_task_dir("t") == Path("/d/alpha/t")
        assert alpha.create_environment("t").data == {"owner": "alpha"}
        assert alpha.syncs_adapter_env_to_state is False
        assert alpha.get_grading_config("t") == "grading-alpha"

        assert beta.get_task_dir("u") == Path("/d/beta/u")
        assert beta.syncs_adapter_env_to_state is True
        assert beta.get_grading_config("u") == "grading-beta"

    def test_single_adapter_returns_self(self) -> None:
        single = _AlphaFake({})
        conductor = _conductor(single)
        # Empty entry (single-adapter run) resolves to the one adapter.
        assert conductor._adapter_for(_spec(entry="")) is single
