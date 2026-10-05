"""The orchestrator classifies a trial's execution mode once per unit.

``_unit_execution_mode`` is the single caller of
:func:`tolokaforge.core.execution_mode.select_execution_mode`. It reads the
``(entry, task_id)`` unit's task-description metadata, memoises the verdict,
and carries it onto every trial spec that unit produces. A broken
``agent_harness_command`` raises from the classifier, re-raised with the unit
coordinates so a multi-entry run names which unit emitted the bad metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.execution_mode import HARNESS_COMMAND_METADATA_KEY, ExecutionMode
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = pytest.mark.unit


def _orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "results"), projects=[str(tmp_path)]
            ),
        ),
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )


def _stub_task_description(orchestrator: Orchestrator, metadata: dict) -> None:
    """Make ``_task_description`` yield a description carrying *metadata*.

    ``_unit_execution_mode`` reads only ``.metadata``; stubbing the resolver
    keeps the test to the classification + memoisation contract without
    loading an adapter.
    """
    orchestrator._task_description = (  # type: ignore[method-assign]
        lambda task_id, entry: make_task_description(task_id=task_id, metadata=metadata)
    )


class TestUnitExecutionMode:
    def test_a_non_blank_harness_command_is_delegated(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        _stub_task_description(orchestrator, {HARNESS_COMMAND_METADATA_KEY: "claude --print"})
        assert orchestrator._unit_execution_mode("alpha", "t1") is ExecutionMode.DELEGATED

    def test_an_absent_command_is_engine_loop(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        _stub_task_description(orchestrator, {})
        assert orchestrator._unit_execution_mode("", "t1") is ExecutionMode.ENGINE_LOOP

    def test_a_blank_command_raises_with_unit_context(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        _stub_task_description(orchestrator, {HARNESS_COMMAND_METADATA_KEY: "   "})
        with pytest.raises(RuntimeError, match=r"unit \('alpha', 't1'\): .*non-blank string"):
            orchestrator._unit_execution_mode("alpha", "t1")

    def test_a_non_string_command_raises_with_unit_context(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        _stub_task_description(orchestrator, {HARNESS_COMMAND_METADATA_KEY: ["claude", "--print"]})
        with pytest.raises(RuntimeError, match=r"unit \('alpha', 't1'\): .*non-blank string"):
            orchestrator._unit_execution_mode("alpha", "t1")

    def test_the_verdict_is_memoised_per_unit(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        calls: list[tuple[str, str]] = []

        def _record(task_id: str, entry: str):
            calls.append((entry, task_id))
            return make_task_description(task_id=task_id)

        orchestrator._task_description = _record  # type: ignore[method-assign]

        orchestrator._unit_execution_mode("alpha", "t1")
        orchestrator._unit_execution_mode("alpha", "t1")
        # One resolve for the unit; the second call reads the memo.
        assert calls == [("alpha", "t1")]
