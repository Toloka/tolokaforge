"""HarborAdapter pinned against the reusable grading-contract suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter

from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.unit

_PACK = Path(__file__).resolve().parents[3] / "examples" / "harbor"
_TASK_ID = "write-release-note"


class TestHarborAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"
    expected_supported_execution_modes = frozenset({ExecutionMode.DELEGATED})

    @pytest.fixture
    def adapter(self) -> HarborAdapter:
        return HarborAdapter({"harbor_tasks_dir": str(_PACK)})

    @pytest.fixture
    def task_and_dir(self, adapter: HarborAdapter) -> tuple[TaskConfig, Path]:
        return adapter.get_task(_TASK_ID), adapter.get_task_dir(_TASK_ID)
