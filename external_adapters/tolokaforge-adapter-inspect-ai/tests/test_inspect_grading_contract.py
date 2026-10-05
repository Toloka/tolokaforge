"""InspectAiAdapter pinned against the reusable grading-contract suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter

from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TASK_ID = "poc_smoke"


class TestInspectAiAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"
    expected_supported_execution_modes = frozenset({ExecutionMode.DELEGATED})

    @pytest.fixture
    def adapter(self) -> InspectAiAdapter:
        return InspectAiAdapter({"inspect_task_dir": str(_FIXTURES)})

    @pytest.fixture
    def task_and_dir(self, adapter: InspectAiAdapter) -> tuple[TaskConfig, Path]:
        return adapter.get_task(_TASK_ID), adapter.get_task_dir(_TASK_ID)

    def test_supported_execution_modes_matches_declared_expectation(
        self, adapter: InspectAiAdapter
    ) -> None:
        # Inspect AI is delegated-only: every trial shells out to `inspect eval`,
        # so the adapter never runs the engine turn loop and declares {DELEGATED}
        # alone — the one adapter for which the suite's "ENGINE_LOOP always
        # present" assertion does not hold.
        modes = adapter.supported_execution_modes
        assert isinstance(modes, frozenset)
        assert all(isinstance(mode, ExecutionMode) for mode in modes)
        assert modes == self.expected_supported_execution_modes
