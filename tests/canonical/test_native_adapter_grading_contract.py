""":class:`NativeAdapter` pinned against the reusable grading-contract suite.

Every capability flag and preferred grader kind inherits the shipped default,
so the subclass overrides only the two fixtures the base declares abstract.
The task under test is a real pack shipped in ``examples/native/``; the
reader methods only parse the pack on disk (no runner spin-up).

The sibling test method below —
:meth:`~TestNativeAdapterGradingContract.test_native_grading_source_reports_the_pack_grading_yaml_when_present`
— sits outside the reusable suite because it locks a Native-specific value
(the :attr:`~GradingSourceKind.ON_DISK` answer with the resolved
``grading.yaml`` path) that only :class:`NativeAdapter` returns for a pack
shipping a sibling grading file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters import NativeAdapter
from tolokaforge.adapters._task_loader import GradingSourceKind, load_task_yaml
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

    def test_native_grading_source_reports_the_pack_grading_yaml_when_present(
        self,
        adapter: NativeAdapter,
        task_and_dir: tuple[TaskConfig, Path],
    ) -> None:
        """The helpdesk_01 fixture ships a sibling ``grading.yaml``; the native
        override reads that file off disk and answers
        :attr:`~GradingSourceKind.ON_DISK` with the resolved path and no
        reason. The default-lock intent —
        :attr:`~GradingSourceKind.UNINTERROGABLE` for a bare adapter — is
        covered by ``_AStubAdapter`` in
        ``tests/unit/adapters/test_grading_contract_defaults.py``.
        """
        task, task_dir = task_and_dir

        source = adapter.grading_source(task, task_dir)

        assert source.kind is GradingSourceKind.ON_DISK
        assert source.path == task_dir / "grading.yaml"
        assert source.reason == ""
