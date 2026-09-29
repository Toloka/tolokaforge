"""InspectAiAdapter pinned against the reusable grading-contract suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter

from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TASK_ID = "poc_smoke"


class TestInspectAiAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"

    @pytest.fixture
    def adapter(self) -> InspectAiAdapter:
        return InspectAiAdapter({"inspect_task_dir": str(_FIXTURES)})

    @pytest.fixture
    def task_and_dir(self, adapter: InspectAiAdapter) -> tuple[TaskConfig, Path]:
        return adapter.get_task(_TASK_ID), adapter.get_task_dir(_TASK_ID)
