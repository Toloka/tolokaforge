""":class:`NativeAdapter` under a real harness pinned against the grading-contract suite.

The bare-native subclass in
:mod:`tests.canonical.test_native_adapter_grading_contract` covers the
:data:`~tolokaforge_coding_harnesses.ENGINE_LOOP` branch; this subclass
constructs :class:`NativeAdapter` with a shipped harness slug so
:meth:`~tolokaforge_coding_harnesses.CodingHarnessAdapterMixin.preferred_grader_kind`
returns ``"test_execution"`` — the same value
:meth:`~tolokaforge_coding_harnesses.CodingHarnessAdapterMixin.emit_test_execution_grading`
sets on the emitted payload — and the alignment invariant exercises against
a real adapter rather than a synthetic stand-in.

The reader assertions only parse the pack on disk; no trial materialises,
so the placeholder ``agent_model`` never reaches a real LLM.
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


class TestNativeAdapterGradingContractUnderHarness(AdapterGradingContractSuite):
    expected_preferred_grader_kind = "test_execution"

    @pytest.fixture
    def adapter(self, tmp_path: Path) -> NativeAdapter:
        return NativeAdapter(
            {
                "base_dir": str(tmp_path),
                "tasks_glob": "**/task.yaml",
                "agent_harness": "claude-code",
                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
            }
        )

    @pytest.fixture
    def task_and_dir(self) -> tuple[TaskConfig, Path]:
        return load_task_yaml(_A_REAL_TASK)
