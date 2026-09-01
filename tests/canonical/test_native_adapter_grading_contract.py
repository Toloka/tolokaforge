""":class:`NativeAdapter` pinned against the reusable grading-contract suite.

Every capability flag and preferred grader kind inherits the shipped default,
so this file overrides only the two fixtures the base declares abstract. The
task under test is a real pack shipped in ``examples/native/``; the reader
methods only parse the pack on disk (no runner spin-up).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters import NativeAdapter
from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.canonical

_A_REAL_TASK = (
    Path(__file__).resolve().parents[2]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


class TestNativeAdapterGradingContract(AdapterGradingContractSuite):
    @pytest.fixture
    def adapter(self, tmp_path: Path) -> NativeAdapter:
        return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "**/task.yaml"})

    @pytest.fixture
    def task_and_dir(self) -> tuple[TaskConfig, Path]:
        return load_task_yaml(_A_REAL_TASK)
