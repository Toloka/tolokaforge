"""``_finalize_run_reports_and_status`` — the completion-status invariant.

Locks that ``run_state.status`` ends ``"completed"`` even when
``_generate_reports`` raises, so a mid-teardown fault cannot leave the file
saying ``"running"`` and trigger a spurious resume prompt on the next
invocation. The two completion-gate booleans fall back to False in that
case. See ADR-0041.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.orchestrator import GradingCompleteness, Orchestrator

pytestmark = pytest.mark.unit


def _an_orchestrator_with_stub_state_manager() -> Orchestrator:
    """A minimally-constructed Orchestrator suitable for finalize testing.

    Bypasses ``__init__`` (which needs a real ``RunConfig`` + adapters) and
    stamps the two attributes the finalize path reads.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch.state_manager = MagicMock()
    return orch


def test_status_ends_completed_when_reports_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _an_orchestrator_with_stub_state_manager()

    def _raising_generate_reports(_output_dir: Path) -> None:
        raise RuntimeError("a broken report writer")

    monkeypatch.setattr(orch, "_generate_reports", _raising_generate_reports)
    monkeypatch.setattr(orch, "_publish_grading_completeness", lambda: None)

    with pytest.raises(RuntimeError, match="a broken report writer"):
        orch._finalize_run_reports_and_status(tmp_path)

    orch.state_manager.mark_run_completed.assert_called_once_with(
        zero_coverage=False, zero_judge_graded=False
    )


def test_status_ends_completed_when_publish_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant holds if ``_publish_grading_completeness`` raises too.

    ``zc`` / ``zjg`` initialize to False before the try so the finally reset
    fires even when ``_publish_grading_completeness`` raises before the two
    completion-gate values are read.
    """
    orch = _an_orchestrator_with_stub_state_manager()
    monkeypatch.setattr(orch, "_generate_reports", lambda _out: None)

    def _raising_publish() -> None:
        raise ValueError("classification blew up")

    monkeypatch.setattr(orch, "_publish_grading_completeness", _raising_publish)

    with pytest.raises(ValueError, match="classification blew up"):
        orch._finalize_run_reports_and_status(tmp_path)

    orch.state_manager.mark_run_completed.assert_called_once_with(
        zero_coverage=False, zero_judge_graded=False
    )


def test_status_ends_completed_when_a_base_exception_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant survives a ``BaseException`` (e.g. ``KeyboardInterrupt``).

    The critic's round-2 note fixed the earlier ``except Exception`` shape:
    that clause never catches a ``BaseException``, so the reset had to move
    into ``finally`` and the completion-gate defaults had to be initialized
    before the try. This test locks that shape.
    """
    orch = _an_orchestrator_with_stub_state_manager()

    def _raising_generate_reports(_output_dir: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(orch, "_generate_reports", _raising_generate_reports)
    monkeypatch.setattr(orch, "_publish_grading_completeness", lambda: None)

    with pytest.raises(KeyboardInterrupt):
        orch._finalize_run_reports_and_status(tmp_path)

    orch.state_manager.mark_run_completed.assert_called_once_with(
        zero_coverage=False, zero_judge_graded=False
    )


def test_a_clean_finalize_pass_stamps_the_derived_completion_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When nothing raises, the gates come from ``grading_completeness``."""
    orch = _an_orchestrator_with_stub_state_manager()
    orch.grading_completeness = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=3,
        judge_errored_trials=3,
    )

    monkeypatch.setattr(orch, "_generate_reports", lambda _out: None)
    monkeypatch.setattr(orch, "_publish_grading_completeness", lambda: None)

    orch._finalize_run_reports_and_status(tmp_path)

    orch.state_manager.mark_run_completed.assert_called_once_with(
        zero_coverage=False, zero_judge_graded=True
    )
