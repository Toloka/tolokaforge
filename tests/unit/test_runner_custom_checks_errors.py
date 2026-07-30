"""Executor-error branches of ``RunnerServiceImpl._grade_custom_checks``.

The seam ``check_executor: CheckExecutor | None = None`` on
``RunnerServiceImpl.__init__`` exists to make these branches testable
without a real ``checks.py`` file or a running executor. The three shapes
this locks:

1. Missing ``artifacts_dir`` (the pack claimed ``custom_checks.enabled``
   but no artifacts were delivered) with ``fail_on_error=True`` — score
   is the 0.0 sentinel and one ``__executor__`` wire entry carries the
   error message.
2. Executor returns a top-level ``error`` (module-load failure, timeout,
   crash) with ``fail_on_error=True`` — score is 0.0 and one
   ``__executor__`` wire entry carries the message.
3. Same, with ``fail_on_error=False`` — score is the -1.0 "not evaluated"
   sentinel so ``combine_grade_components`` excludes it from the weighted
   total; the ``__executor__`` audit entry is still emitted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.core.grading.check_runner import InMemoryCheckExecutor
from tolokaforge.core.grading.checks_interface import CheckResultSet
from tolokaforge.runner.models import GradingConfig, InitialStateConfig
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit


class _StubDBClient:
    """Minimal db_client stub — only ``get_state`` is exercised by the seam."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._state = state or {}

    async def get_state(self, trial_id: str) -> Any:
        return SimpleNamespace(data=self._state)


def _trial_context(*, custom_checks: dict[str, Any]) -> SimpleNamespace:
    """Build a bare ``TrialContextRuntime`` shape for the branches under test."""
    task_description = SimpleNamespace(
        task_id="reconcile:0",
        name="reconcile",
        description="",
        category="",
        initial_state=InitialStateConfig(),
    )
    grading_config = GradingConfig(custom_checks=custom_checks)
    return SimpleNamespace(
        task_description=task_description,
        grading_config=grading_config,
    )


def _build_servicer(*, check_executor: Any) -> RunnerServiceImpl:
    """Construct a real ``RunnerServiceImpl`` so ``self._loop`` is wired."""
    return RunnerServiceImpl(db_client=_StubDBClient(), check_executor=check_executor)


def _await(servicer: RunnerServiceImpl, coro: Any) -> Any:
    """Drive ``_grade_custom_checks`` on the servicer's dedicated loop."""
    return servicer._run_async(coro)


class TestGradeCustomChecksExecutorErrors:
    def test_missing_artifacts_dir_fail_on_error_true_emits_zero_sentinel(self) -> None:
        servicer = _build_servicer(check_executor=InMemoryCheckExecutor())
        try:
            trial_id = "reconcile:0"
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": True,
                },
            )

            score, wire = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == 0.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "no artifacts_dir" in entry.message
            assert trial_id in entry.message
        finally:
            servicer.shutdown()

    def test_missing_artifacts_dir_fail_on_error_false_emits_negative_sentinel(self) -> None:
        servicer = _build_servicer(check_executor=InMemoryCheckExecutor())
        try:
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": False,
                },
            )

            score, wire = _await(
                servicer,
                servicer._grade_custom_checks("reconcile:0", trial_context, llm_messages=[]),
            )

            assert score == -1.0
            assert len(wire) == 1
            assert wire[0].check_name == "__executor__"
            assert wire[0].status == "error"
        finally:
            servicer.shutdown()

    def test_executor_error_fail_on_error_true_emits_zero_sentinel(self, tmp_path: Path) -> None:
        executor = InMemoryCheckExecutor(return_error="module load failed: syntax error at :12")
        servicer = _build_servicer(check_executor=executor)
        try:
            trial_id = "reconcile:0"
            # Artifacts dir must exist so the seam gets past the missing-dir branch
            # and dispatches to the executor.
            servicer._artifact_dirs[trial_id] = tmp_path
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": True,
                },
            )

            score, wire = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == 0.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "module load failed" in entry.message
            # The executor was actually dispatched.
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()

    def test_executor_error_fail_on_error_false_emits_negative_sentinel(
        self, tmp_path: Path
    ) -> None:
        executor = InMemoryCheckExecutor(
            result_set=CheckResultSet(error="timeout after 30s", execution_time_ms=30_000)
        )
        # ``return_error`` and ``result_set`` are mutually exclusive on the fixture,
        # but a preset ``result_set`` with ``error`` set carries the same shape.
        servicer = _build_servicer(check_executor=executor)
        try:
            trial_id = "reconcile:0"
            servicer._artifact_dirs[trial_id] = tmp_path
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": False,
                },
            )

            score, wire = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == -1.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "timeout after 30s" in entry.message
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()
