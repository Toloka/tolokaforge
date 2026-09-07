""":class:`TerminalBenchAdapter` pinned against the reusable grading-contract suite.

``requires_docker_cli_in_runner`` is the capability flag that flips off the
shipped default (:class:`TerminalBenchAdapter` sets it ``True``); the other
two flags inherit the base ``False`` default. The preferred grader kind is
``"test_execution"`` on both loop branches because the pack's own verifier
writes ``/logs/verifier/reward.txt`` regardless of whether the CLI or the
engine loop drove the trial. ``task_and_dir`` reuses the real shipped pack
at ``examples/terminal_bench/fix-airline-segmentation/`` — canonical
``{task.yaml, docker-compose.yaml, tests/}`` shape
:meth:`TerminalBenchAdapter.get_task_ids` walks. The reader assertions only
parse the pack on disk (no runner spin-up).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.unit

_TERMINAL_BENCH_DIR = Path(__file__).resolve().parents[3] / "examples" / "terminal_bench"
_TASK_ID = "fix-airline-segmentation"


class TestTerminalBenchAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"

    @pytest.fixture
    def adapter(self) -> TerminalBenchAdapter:
        return TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(_TERMINAL_BENCH_DIR),
                "task_ids": [_TASK_ID],
                "prebuild_images": False,
            }
        )

    @pytest.fixture
    def task_and_dir(self, adapter: TerminalBenchAdapter) -> tuple[TaskConfig, Path]:
        return adapter.get_task(_TASK_ID), adapter.get_task_dir(_TASK_ID)
