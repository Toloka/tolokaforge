"""Unit tests for the orchestrator's execution-mode capability gate.

A run that names a coding harness selects the delegated execution mode. The
gate in ``Orchestrator.load_tasks`` refuses that run — before any task ids
are fetched or any container work starts — against an adapter that does not
run the delegated mode, naming both sides. ``adapter_supported_modes`` is the
decision function the gate reads; it honours a legacy
``supports_coding_harness = True`` for one release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.models.run_config import HarnessAdapterConfig
from tolokaforge.core.orchestrator import Orchestrator, adapter_supported_modes

pytestmark = pytest.mark.unit


class _EngineLoopOnlyAdapter(BaseAdapter):
    """A bare adapter: inherits the default ``{ENGINE_LOOP}``, no harness opt-in."""

    def get_task_ids(self) -> list[str]:
        return []

    def get_task(self, task_id: str) -> Any:  # pragma: no cover - gate fires first
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


def _delegated_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4", harness="claude-code")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/execution_mode_gate"),
    )


def _harness_via_params_run_config(agent_harness: str) -> RunConfig:
    """A run whose harness is spelled ONLY via the legacy params address.

    ``models.agent.harness`` is left unset and the selector rides
    ``evaluation.harness_adapter.params.agent_harness`` — the shape this repo's
    matrix workflow and the terminal-bench recipes still write. Passing a built
    ``EvaluationConfig`` (not a dict) keeps the parse-time alias lift from
    folding the param into ``models.agent.harness``, so the gate must read the
    param address on its own.
    """
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            output_dir="/tmp/execution_mode_gate",
            harness_adapter=HarnessAdapterConfig(
                type="terminal_bench", params={"agent_harness": agent_harness}
            ),
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
        # legacy-flag derivation: the identity check sees a frozenset distinct
        # from BaseAdapter's default and reads it verbatim, so the legacy flag
        # does not add DELEGATED back.
        class _ExplicitEngineLoopOnly(_EngineLoopOnlyAdapter):
            supports_coding_harness = True
            supported_execution_modes = frozenset({ExecutionMode.ENGINE_LOOP})

        assert ExecutionMode.DELEGATED not in adapter_supported_modes(_ExplicitEngineLoopOnly({}))


class TestDelegatedGate:
    def test_engine_loop_only_adapter_is_refused(self) -> None:
        orch = Orchestrator(_delegated_run_config())
        orch.adapter = _EngineLoopOnlyAdapter({})

        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        # Both sides of the mismatch are named, plus the accepted mode set.
        assert "claude-code" in message
        assert ExecutionMode.DELEGATED.value in message
        assert ExecutionMode.ENGINE_LOOP.value in message

    def test_legacy_flag_adapter_clears_the_gate(self) -> None:
        orch = Orchestrator(_delegated_run_config())
        orch.adapter = _LegacyHarnessAdapter({})

        # The gate's condition is what "passes" means; the legacy flag derives
        # delegated support, so the gate does not refuse this adapter.
        assert ExecutionMode.DELEGATED in adapter_supported_modes(orch.adapter)

    def test_harness_via_legacy_params_only_is_refused(self) -> None:
        # The harness is spelled ONLY via
        # evaluation.harness_adapter.params.agent_harness — not models.agent.harness.
        # The gate keys on _configured_harness, which reads that address too, so
        # an engine-loop-only adapter is still refused.
        orch = Orchestrator(_harness_via_params_run_config("claude-code"))
        orch.adapter = _EngineLoopOnlyAdapter({})

        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        # Both sides of the mismatch are named, plus the accepted mode set.
        assert "claude-code" in message
        assert ExecutionMode.DELEGATED.value in message
        assert ExecutionMode.ENGINE_LOOP.value in message

    def test_engine_loop_sentinel_via_params_is_not_refused(self) -> None:
        # The engine-loop sentinel is not a coding harness: it selects no
        # delegated mode, so the gate never fires — the engine loop runs on
        # any adapter, including this engine-loop-only one with no tasks.
        orch = Orchestrator(_harness_via_params_run_config("engine-loop"))
        orch.adapter = _EngineLoopOnlyAdapter({})

        # No RuntimeError: the gate is skipped and the empty run loads cleanly.
        assert orch.load_tasks() is None
