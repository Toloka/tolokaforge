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

The fourth shape is not an error at all: a suite that ran and *decided nothing*,
because every check skipped or the file declared none. It also reports the -1.0
sentinel — the runner's spelling of the unscored component the core engine
produces for the same suite — and the case that reached a verdict is asserted
beside it so the sentinel is not a constant.

Every one of the four also supplies the sentence ``Grade.reasons`` carries for this
component, which is why each shape asserts the third element beside its score: a
component that scored nothing and said nothing leaves the trial's grade asserting
that no component was evaluated, and the error text is the only thing that says why
the suite produced no verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.core.grading.check_runner import InMemoryCheckExecutor
from tolokaforge.core.grading.checks_interface import CheckResult, CheckResultSet, CheckStatus
from tolokaforge.runner.models import RunnerGradingConfig, RunnerInitialStateConfig
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit

#: What every sentence describing a suite that never ran opens with.
_FAILED_TO_RUN = "Custom checks: the suite failed to run — "


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
        initial_state=RunnerInitialStateConfig(),
    )
    grading_config = RunnerGradingConfig(custom_checks=custom_checks)
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

            score, wire, reason = _await(
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
            # The wire entry preserves the audit; the sentence is what the grade reads.
            assert reason == f"{_FAILED_TO_RUN}{entry.message}"
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

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks("reconcile:0", trial_context, llm_messages=[]),
            )

            assert score == -1.0
            assert len(wire) == 1
            assert wire[0].check_name == "__executor__"
            assert wire[0].status == "error"
            # Unscored and still accounted for: the fold names the component that
            # produced no verdict, and this is what says why it produced none.
            assert reason == f"{_FAILED_TO_RUN}{wire[0].message}"
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

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == 0.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "module load failed" in entry.message
            assert reason == f"{_FAILED_TO_RUN}module load failed: syntax error at :12"
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

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == -1.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "timeout after 30s" in entry.message
            assert reason == f"{_FAILED_TO_RUN}timeout after 30s"
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()

    def test_executor_raises_fail_on_error_true_emits_zero_sentinel(self, tmp_path: Path) -> None:
        # An executor that raises rather than capturing the error into
        # :class:`CheckResultSet` is a contract violation; the seam converts
        # the exception to the sentinel-entry shape so the audit survives
        # and does not escape to the outer trial handler.
        boom = RuntimeError("boom: executor crashed mid-run")
        executor = InMemoryCheckExecutor(raise_on_run=boom)
        servicer = _build_servicer(check_executor=executor)
        try:
            trial_id = "reconcile:0"
            servicer._artifact_dirs[trial_id] = tmp_path
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": True,
                },
            )

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == 0.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "boom: executor crashed mid-run" in entry.message
            assert reason == f"{_FAILED_TO_RUN}boom: executor crashed mid-run"
            # Dispatch happened; the call is recorded even though it raised.
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()

    @pytest.mark.parametrize(
        ("results", "expected", "expected_reason"),
        (
            pytest.param(
                [
                    CheckResult(check_name="c1", status=CheckStatus.SKIPPED, score=0.0),
                    CheckResult(check_name="c2", status=CheckStatus.SKIPPED, score=1.0),
                ],
                -1.0,
                "Custom checks: no check reached a verdict — all 2 skipped",
                id="every_check_skipped",
            ),
            pytest.param(
                [],
                -1.0,
                "Custom checks: no check reached a verdict — the file declared no check",
                id="the_file_declared_no_check",
            ),
            pytest.param(
                [
                    CheckResult(check_name="c1", status=CheckStatus.SKIPPED, score=1.0),
                    CheckResult(
                        check_name="c2",
                        status=CheckStatus.FAILED,
                        score=0.0,
                        message="the ledger did not balance",
                    ),
                ],
                0.0,
                "Custom checks: score=0.00, 1 of 1 checks failed, 1 skipped — "
                "c2: the ledger did not balance",
                id="one_check_reached_a_verdict",
            ),
        ),
    )
    def test_a_suite_that_decided_nothing_reports_the_not_evaluated_sentinel(
        self,
        tmp_path: Path,
        results: list[CheckResult],
        expected: float,
        expected_reason: str,
    ) -> None:
        """The runner reaches core's answer for a suite of skips, through one predicate.

        ``aggregate_score`` averages the unskipped results and returns ``0.0`` over none
        of them, so a suite that only skipped would fold as a component that failed —
        the vacuous ``1.0`` sign-flipped, and a divergence from the core engine, which
        leaves the component unscored. The runner's ``-1.0`` is the same answer in this
        substrate's own vocabulary: ``combine_grade_components`` reads it as not
        evaluated.

        The third row is what keeps the sentinel from being a constant: one failing
        check among skips *decided* something, its aggregate is also ``0.0``, and that
        ``0.0`` reaches the fold as a real verdict. The sentence separates the two
        answers the score cannot: an unscored component reads as a suite that decided
        nothing, and a scored ``0.0`` names the check that lost.
        """
        executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=results))
        servicer = _build_servicer(check_executor=executor)
        try:
            trial_id = "reconcile:0"
            servicer._artifact_dirs[trial_id] = tmp_path
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": True,
                },
            )

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == expected
            assert reason == expected_reason
            assert [entry.check_name for entry in wire] == [item.check_name for item in results]
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()

    def test_executor_raises_fail_on_error_false_emits_negative_sentinel(
        self, tmp_path: Path
    ) -> None:
        boom = RuntimeError("boom: executor crashed mid-run")
        executor = InMemoryCheckExecutor(raise_on_run=boom)
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

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert score == -1.0
            assert len(wire) == 1
            entry = wire[0]
            assert entry.check_name == "__executor__"
            assert entry.status == "error"
            assert "boom: executor crashed mid-run" in entry.message
            assert reason == f"{_FAILED_TO_RUN}boom: executor crashed mid-run"
            assert len(executor.call_log.runs) == 1
        finally:
            servicer.shutdown()

    @pytest.mark.parametrize(
        ("build_executor", "artifacts_delivered"),
        (
            pytest.param(InMemoryCheckExecutor, False, id="no_artifacts_dir"),
            pytest.param(
                lambda: InMemoryCheckExecutor(return_error="module load failed"),
                True,
                id="the_executor_reported_an_error",
            ),
            pytest.param(
                lambda: InMemoryCheckExecutor(raise_on_run=RuntimeError("boom")),
                True,
                id="the_executor_raised",
            ),
            pytest.param(
                lambda: InMemoryCheckExecutor(result_set=CheckResultSet(results=[])),
                True,
                id="the_suite_decided_nothing",
            ),
        ),
    )
    def test_no_return_that_reached_the_component_leaves_it_unaccounted_for(
        self,
        tmp_path: Path,
        build_executor: Callable[[], InMemoryCheckExecutor],
        artifacts_delivered: bool,
    ) -> None:
        """Every shape above, asked one question: is there a sentence at all?

        The per-shape cells pin what each says; this one is the quantifier over the
        return statements, so a branch added later — or one that starts returning the
        empty string — is a red rather than a component that scored nothing and
        explained nothing.
        """
        servicer = _build_servicer(check_executor=build_executor())
        try:
            trial_id = "reconcile:0"
            if artifacts_delivered:
                servicer._artifact_dirs[trial_id] = tmp_path
            trial_context = _trial_context(
                custom_checks={
                    "enabled": True,
                    "file": "checks.py",
                    "interface_version": "1.0",
                    "fail_on_error": True,
                },
            )

            _, _, reason = _await(
                servicer,
                servicer._grade_custom_checks(trial_id, trial_context, llm_messages=[]),
            )

            assert reason, "the component produced no verdict and no account of why"
            assert reason.startswith("Custom checks: "), reason
        finally:
            servicer.shutdown()

    def test_a_pack_with_no_suite_to_describe_supplies_no_sentence(self) -> None:
        """The one return that says nothing, because there is nothing to say.

        A disabled block is the boundary of "never empty": the component is left
        unscored and the ledger files its own skip note, so a sentence here would be
        a second account of the same absence.
        """
        servicer = _build_servicer(check_executor=InMemoryCheckExecutor())
        try:
            trial_context = _trial_context(custom_checks={"enabled": False, "file": "checks.py"})

            score, wire, reason = _await(
                servicer,
                servicer._grade_custom_checks("reconcile:0", trial_context, llm_messages=[]),
            )

            assert (score, wire, reason) == (-1.0, [], None)
        finally:
            servicer.shutdown()
