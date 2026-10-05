"""Unit tests for the orchestrator's execution-mode capability gate.

The gate keys off the single per-``(entry, task)`` mode decision: the mode is
classified from the task description's ``agent_harness_command`` metadata — the
same signal the conductor dispatches on — and
``Orchestrator._gate_execution_mode_capability`` refuses a unit whose owning
adapter does not run that mode, naming the unit, the adapter, the mode, and the
modes the adapter does run. It fires in the ``run()`` / ``run_worker()``
pre-flight window, after task descriptions are materialised and before any
container is provisioned. ``adapter_supported_modes`` is the decision function
the gate reads; it honours a legacy ``supports_coding_harness = True`` for one
release.

One capability gate serves both the single-adapter run (the empty-string
entry) and the multi-harness matrix (one unit per entry/task). The separate
per-entry ``_gate_entry_execution_mode`` keeps only the engine-loop +
per-entry-``model.agent`` refusal (#1769).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_task_config, make_task_description
from tolokaforge.adapters import register_adapter
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.execution_mode import HARNESS_COMMAND_METADATA_KEY, ExecutionMode
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, adapter_supported_modes

pytestmark = pytest.mark.unit


class _EngineLoopOnlyAdapter(BaseAdapter):
    """A bare adapter: inherits the default ``{ENGINE_LOOP}``, no harness opt-in."""

    def get_task_ids(self) -> list[str]:
        return []

    def get_task(self, task_id: str) -> Any:  # pragma: no cover - no tasks served
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError


class _LegacyHarnessAdapter(_EngineLoopOnlyAdapter):
    """Pre-capability adapter: sets only the legacy string flag, no mode override."""

    supports_coding_harness = True


# ---------------------------------------------------------------------------
# Task-serving fakes: the capability gate classifies from description metadata,
# so the fixtures must materialise tasks whose ``agent_harness_command`` is set
# (delegated) or absent (engine-loop).
# ---------------------------------------------------------------------------


class _TaskServingAdapter(_EngineLoopOnlyAdapter):
    """Serves one task; subclass knobs set its metadata and supported modes.

    ``_harness_command`` (when set) rides the task description's
    ``agent_harness_command`` metadata, which classifies the unit delegated.
    """

    _adapter_type = "fake_b3"
    _ids: list[str] = ["t1"]
    _harness_command: str | None = None

    def get_task_ids(self) -> list[str]:
        return list(self._ids)

    def get_task(self, task_id: str) -> Any:
        return make_task_config(task_id=task_id)

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/fake") / self._adapter_type / task_id

    def to_task_description(self, task_id: str) -> Any:
        metadata = (
            {HARNESS_COMMAND_METADATA_KEY: self._harness_command}
            if self._harness_command is not None
            else {}
        )
        return make_task_description(
            task_id=task_id, adapter_type=self._adapter_type, metadata=metadata
        )


class _EngineLoopEmittingAdapter(_TaskServingAdapter):
    """Emits a harness command but declares only the engine loop — the newly
    catchable mismatch: delegated metadata on an engine-loop-only adapter."""

    _adapter_type = "fake_b3_engine_emit"
    _ids = ["engine-emit-1"]
    _harness_command = "claude --print"


class _DelegatedEmittingAdapter(_TaskServingAdapter):
    """Emits a harness command and declares the delegated mode — classifies
    delegated, runs delegated: the gate clears it."""

    _adapter_type = "fake_b3_deleg_emit"
    _ids = ["deleg-emit-1"]
    _harness_command = "claude --print"
    supported_execution_modes = frozenset({ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED})


class _LegacyEmittingAdapter(_TaskServingAdapter):
    """Emits a harness command and carries only the legacy ``supports_coding_harness``
    flag — the back-compat derivation adds delegated, so the gate clears it."""

    _adapter_type = "fake_b3_legacy_emit"
    _ids = ["legacy-emit-1"]
    _harness_command = "claude --print"
    supports_coding_harness = True


class _PlainEngineLoopAdapter(_TaskServingAdapter):
    """Serves a task with no harness command — classifies engine-loop even when
    the run config names a harness slug: no spurious refusal, no divergence."""

    _adapter_type = "fake_b3_plain"
    _ids = ["plain-1"]
    _harness_command = None


def _single_adapter_config(adapter_type: str, *, harness: str | None = None) -> RunConfig:
    agent = ModelConfig(provider="openai", name="gpt-4", harness=harness)
    return RunConfig(
        models={"agent": agent},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            output_dir="/tmp/execution_mode_gate",
            harness_adapter={"type": adapter_type},
        ),
    )


class TestAdapterSupportedModes:
    def test_engine_loop_only_adapter_omits_delegated(self) -> None:
        modes = adapter_supported_modes(_EngineLoopOnlyAdapter({}))
        assert modes == frozenset({ExecutionMode.ENGINE_LOOP})

    def test_overriding_adapter_is_read_verbatim(self) -> None:
        class _Both(_EngineLoopOnlyAdapter):
            supported_execution_modes = frozenset(
                {ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED}
            )

        assert ExecutionMode.DELEGATED in adapter_supported_modes(_Both({}))

    def test_legacy_flag_derives_delegated_when_not_overridden(self) -> None:
        # Back-compat: an external adapter that predates the capability and
        # sets supports_coding_harness=True must still clear the gate.
        assert ExecutionMode.DELEGATED in adapter_supported_modes(_LegacyHarnessAdapter({}))

    def test_explicit_engine_loop_only_opts_out_despite_legacy_flag(self) -> None:
        # An explicit supported_execution_modes declaration beats the
        # legacy-flag derivation: the MRO check sees a class below BaseAdapter
        # that defines the capability in its own __dict__ and reads it verbatim,
        # so the legacy flag does not add DELEGATED back.
        class _ExplicitEngineLoopOnly(_EngineLoopOnlyAdapter):
            supports_coding_harness = True
            supported_execution_modes = frozenset({ExecutionMode.ENGINE_LOOP})

        assert ExecutionMode.DELEGATED not in adapter_supported_modes(_ExplicitEngineLoopOnly({}))


class TestDelegatedGate:
    """The single-adapter capability pass (the empty-string entry).

    The adapter is built from ``evaluation.harness_adapter.type`` the way the
    real single-adapter path builds it, so the refusal names that config type.
    """

    def _gated(self, adapter_type: str, *, harness: str | None = None) -> Orchestrator:
        orch = Orchestrator(_single_adapter_config(adapter_type, harness=harness))
        orch.load_tasks()
        return orch

    def test_adapter_emitting_command_but_engine_loop_only_is_refused(self) -> None:
        # Newly catchable: the task metadata carries ``agent_harness_command``,
        # so the unit classifies delegated — but the adapter runs only the
        # engine loop. The config names no harness slug, so the old run-level
        # slug gate would have let this slip through; the metadata gate refuses.
        orch = self._gated("fake_b3_engine_emit")

        with pytest.raises(RuntimeError) as excinfo:
            orch._gate_execution_mode_capability()

        message = str(excinfo.value)
        assert "engine-emit-1" in message
        assert "fake_b3_engine_emit" in message
        assert ExecutionMode.DELEGATED.value in message
        assert ExecutionMode.ENGINE_LOOP.value in message

    def test_adapter_emitting_command_with_delegated_support_clears_gate(self) -> None:
        orch = self._gated("fake_b3_deleg_emit")
        assert orch._gate_execution_mode_capability() is None

    def test_legacy_flag_adapter_emitting_command_clears_gate(self) -> None:
        # The legacy flag derives delegated support, so a delegated-classifying
        # unit on it is not refused.
        orch = self._gated("fake_b3_legacy_emit")
        assert orch._gate_execution_mode_capability() is None

    def test_config_harness_slug_but_no_command_is_not_refused(self) -> None:
        # The run config names a coding harness, but the adapter emits no
        # command, so the unit classifies engine-loop in both the gate and
        # dispatch. The gate must not refuse on the slug alone — no divergence.
        orch = self._gated("fake_b3_plain", harness="claude-code")
        assert orch._gate_execution_mode_capability() is None

    def test_plain_engine_loop_adapter_clears_gate(self) -> None:
        orch = self._gated("fake_b3_plain")
        assert orch._gate_execution_mode_capability() is None


# ---------------------------------------------------------------------------
# Multi-harness per-(entry, task) capability gate
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_b3_fakes() -> None:
    register_adapter("fake_b3_engine_emit", _EngineLoopEmittingAdapter)
    register_adapter("fake_b3_deleg_emit", _DelegatedEmittingAdapter)
    register_adapter("fake_b3_legacy_emit", _LegacyEmittingAdapter)
    register_adapter("fake_b3_plain", _PlainEngineLoopAdapter)
    # Engine-loop-only adapter that serves no tasks: used by the #1769 refusal
    # tests, which fire during composite build, before any task enumeration.
    register_adapter("fake_b3_engine", _EngineLoopOnlyAdapter)
    register_adapter("fake_b3_delegated", _DelegatedEmittingAdapter)


def _multi_harness_config(entries: list[dict[str, Any]]) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/execution_mode_gate"),
        harnesses={"entries": entries},
    )


class TestMultiHarnessEntryGate:
    def _gated(self, entries: list[dict[str, Any]]) -> Orchestrator:
        orch = Orchestrator(_multi_harness_config(entries))
        orch.load_tasks()
        return orch

    def test_one_bad_entry_fails_loud(self) -> None:
        # The entry's adapter emits a harness command but runs only the engine
        # loop; the unit classifies delegated and is refused, naming the entry,
        # the adapter, and the modes.
        orch = self._gated([{"name": "bad", "adapter": "fake_b3_engine_emit"}])

        with pytest.raises(RuntimeError) as excinfo:
            orch._gate_execution_mode_capability()

        message = str(excinfo.value)
        assert "bad" in message
        assert "fake_b3_engine_emit" in message
        assert ExecutionMode.DELEGATED.value in message
        assert ExecutionMode.ENGINE_LOOP.value in message

    def test_valid_sibling_does_not_mask_a_bad_entry(self) -> None:
        # The valid delegated-capable entry is listed first; the gate must still
        # refuse the bad sibling rather than passing because a good unit cleared.
        orch = self._gated(
            [
                {"name": "good", "adapter": "fake_b3_deleg_emit"},
                {"name": "bad", "adapter": "fake_b3_engine_emit"},
            ]
        )
        with pytest.raises(RuntimeError, match="bad"):
            orch._gate_execution_mode_capability()

    def test_all_valid_entries_load_cleanly(self) -> None:
        orch = self._gated(
            [
                {"name": "deleg", "adapter": "fake_b3_deleg_emit"},
                {"name": "plain", "adapter": "fake_b3_plain"},
            ]
        )
        assert orch._gate_execution_mode_capability() is None


class TestEngineLoopPerEntryAgentModel:
    """An engine-loop entry may not carry a per-entry ``model.agent`` (#1769).

    This refusal is independent of the capability gate: it fires during
    composite build, from ``_gate_entry_execution_mode``.
    """

    _AGENT_OVERRIDE = {"agent": {"provider": "openai", "name": "gpt-4o"}}

    def test_engine_loop_entry_with_per_entry_agent_model_is_refused(self) -> None:
        orch = Orchestrator(
            _multi_harness_config(
                [
                    {
                        "name": "engine-leg",
                        "adapter": "fake_b3_engine",
                        "mode": "engine_loop",
                        "model": self._AGENT_OVERRIDE,
                    }
                ]
            )
        )
        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        assert "engine-leg" in message
        assert "#1769" in message

    def test_inferred_engine_loop_entry_with_agent_model_is_refused(self) -> None:
        # No explicit mode: the agent model carries no harness, so the entry
        # infers engine-loop — and still trips the per-entry-agent refusal.
        orch = Orchestrator(
            _multi_harness_config(
                [
                    {
                        "name": "inferred-leg",
                        "adapter": "fake_b3_engine",
                        "model": self._AGENT_OVERRIDE,
                    }
                ]
            )
        )
        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        assert "inferred-leg" in message
        assert "#1769" in message

    def test_delegated_entry_with_per_entry_agent_model_is_allowed(self) -> None:
        # A delegated entry honours its per-entry agent model (it flows through
        # the harness command), so the refusal must not fire.
        orch = Orchestrator(
            _multi_harness_config(
                [
                    {
                        "name": "delegated-leg",
                        "adapter": "fake_b3_delegated",
                        "mode": "delegated",
                        "model": self._AGENT_OVERRIDE,
                    }
                ]
            )
        )
        assert orch.load_tasks() is None

    def test_engine_loop_entry_without_agent_model_is_allowed(self) -> None:
        orch = Orchestrator(
            _multi_harness_config(
                [
                    {
                        "name": "engine-leg",
                        "adapter": "fake_b3_engine",
                        "mode": "engine_loop",
                    }
                ]
            )
        )
        assert orch.load_tasks() is None
