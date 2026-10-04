""":class:`HarborAdapter` pinned against the reusable grading-contract suite.

``requires_docker_cli_in_runner`` is the capability flag that flips off the
shipped default (:class:`HarborAdapter` sets it ``True``); the other two flags
inherit the base ``False`` default. ``supported_execution_modes`` carries both
``ENGINE_LOOP`` and ``DELEGATED`` — the engine loop runs a turn loop, a
delegated harness runs its own CLI, and both read the pack's reward. The
preferred grader kind is ``"test_execution"`` on both branches because the
Harbor verifier writes ``/logs/verifier/reward.txt`` regardless. ``task_and_dir``
reuses the vendored example pack at ``examples/harbor/write-release-note/`` —
the canonical ``{task.toml, environment/, tests/test.sh}`` Harbor shape. The
reader assertions only parse the pack on disk (no runner spin-up).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter

from tolokaforge.adapters import adapter_class, available_adapters, get_adapter
from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

pytestmark = pytest.mark.unit

_HARBOR_TASKS_DIR = Path(__file__).resolve().parents[3] / "examples" / "harbor"
_TASK_ID = "write-release-note"


def _params() -> dict:
    return {
        "harbor_tasks_dir": str(_HARBOR_TASKS_DIR),
        "task_ids": [_TASK_ID],
        "prebuild_images": False,
    }


def test_harbor_is_registered() -> None:
    assert "harbor" in available_adapters()
    assert adapter_class("harbor") is HarborAdapter


def test_get_adapter_resolves_harbor() -> None:
    adapter = get_adapter("harbor", _params())
    assert isinstance(adapter, HarborAdapter)
    assert adapter.get_task_ids() == [_TASK_ID]


class TestHarborAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"
    expected_supported_execution_modes = frozenset(
        {ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED}
    )

    @pytest.fixture
    def adapter(self) -> HarborAdapter:
        return HarborAdapter(_params())

    @pytest.fixture
    def task_and_dir(self, adapter: HarborAdapter) -> tuple[TaskConfig, Path]:
        return adapter.get_task(_TASK_ID), adapter.get_task_dir(_TASK_ID)
