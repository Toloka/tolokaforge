"""``composite.grade_custom_checks`` — end-to-end tuple parity lock.

The composite owns ``custom_checks`` end-to-end (config normalisation,
artifacts-dir gating, degrade-to-empty on DB failure, executor drive,
wire-result wrapping). This suite constructs an
:class:`InProcessGradingSubstrate` over a hand-built ``{initial_tables,
final_tables}`` pair, drives :func:`composite.grade_custom_checks` against
a fixture pack whose ``checks.py`` decides ``passed`` / ``failed`` /
``skipped`` on the trial's evidence, and asserts the returned
``(score, wire_results, reason)`` tuple matches what the runner path used
to produce for the same evidence — byte-for-byte.

The four shapes the runner's ``_grade_custom_checks`` returned pre-extraction:

- **disabled** — the pack declared ``enabled: false`` (or no block). The
  tuple is ``(-1.0, [], None)`` — no suite to describe.
- **verdicts** — the executor ran and every check reached
  ``passed`` / ``failed``. Score is ``CheckResultSet.aggregate_score``, wire
  entries mirror the results, and ``reasons`` renders per
  :func:`custom_checks_reason`.
- **all-skipped** — the executor ran and every check skipped. Score is
  ``-1.0`` (the ``decided_something`` gate); the wire keeps the skip
  entries for audit.
- **missing-artifacts** — ``enabled: true`` but no ``checks.py`` delivered.
  Score follows ``fail_on_error``; wire carries one ``__executor__``
  sentinel; ``reasons`` opens with the ``failed to run`` sentence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading import composite
from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.runner.models import RunnerInitialStateConfig, TaskDescription

pytestmark = pytest.mark.canonical

_CHECKS_PY = """\
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckFailed, CheckPassed, check, init,
)


_ctx: CheckContext | None = None


@init(interface_version="1.0")
def _load(ctx: CheckContext) -> None:
    global _ctx
    _ctx = ctx


@check
def orders_shipped():
    assert _ctx is not None
    orders = _ctx.final_state.data.get("orders", [])
    if orders and orders[0].get("status") == "shipped":
        return CheckPassed("order shipped")
    return CheckFailed("not shipped")


@check
def user_named_alice():
    assert _ctx is not None
    users = _ctx.final_state.data.get("users", [])
    if users and users[0].get("name") == "Alice":
        return CheckPassed("alice present")
    return CheckFailed("not alice")
"""

_ALL_SKIP_CHECKS_PY = """\
from tolokaforge.core.grading.checks_interface import CheckSkipped, check, init


@init(interface_version="1.0")
def _load(ctx):
    pass


@check
def a():
    return CheckSkipped("always skipped")


@check
def b():
    return CheckSkipped("always skipped")
"""


def _task_description() -> TaskDescription:
    """Minimal ``TaskDescription`` — only fields the composite reads matter."""
    return TaskDescription(
        task_id="reconcile:0",
        name="reconcile",
        category="test",
        description="Ledger reconciliation.",
        adapter_type="tau",
        system_prompt="",
        initial_state=RunnerInitialStateConfig(),
    )


def _substrate(*, final_tables: dict[str, Any] | None = None) -> InProcessGradingSubstrate:
    """Substrate exposing only the reads ``grade_custom_checks`` touches."""
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state=final_tables if final_tables is not None else {},
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-composite-grade-custom-checks")


def _write_checks(tmp_path: Path, body: str) -> Path:
    """Drop ``checks.py`` under ``tmp_path`` and return the artifacts dir."""
    (tmp_path / "checks.py").write_text(body, encoding="utf-8")
    return tmp_path


class TestGradeCustomChecksMatchesRunnerPath:
    """Every tuple the composite produces matches what the runner path used
    to produce for the same evidence — the extraction is behaviour-preserving.
    """

    def test_verdicts_return_scored_tuple_with_per_check_wire(self, tmp_path: Path) -> None:
        artifacts_dir = _write_checks(tmp_path, _CHECKS_PY)
        final_state = {
            "orders": [{"id": "o1", "status": "shipped"}],
            "users": [{"id": "u1", "name": "Alice"}],
        }
        score, wire, reason = composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=_substrate(final_tables=final_state),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=artifacts_dir,
            check_executor=CheckRunner(),
            logger=_logger(),
        )
        assert score == pytest.approx(1.0)
        assert [entry.check_name for entry in wire] == ["orders_shipped", "user_named_alice"]
        assert all(entry.status == "passed" for entry in wire)
        assert reason is not None
        assert reason == "Custom checks: score=1.00, all 2 checks passed"

    def test_mixed_verdicts_return_averaged_score(self, tmp_path: Path) -> None:
        artifacts_dir = _write_checks(tmp_path, _CHECKS_PY)
        final_state = {
            "orders": [{"id": "o1", "status": "pending"}],
            "users": [{"id": "u1", "name": "Alice"}],
        }
        score, wire, reason = composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=_substrate(final_tables=final_state),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=artifacts_dir,
            check_executor=CheckRunner(),
            logger=_logger(),
        )
        assert score == pytest.approx(0.5)
        by_name = {entry.check_name: entry for entry in wire}
        assert by_name["orders_shipped"].status == "failed"
        assert by_name["user_named_alice"].status == "passed"
        assert reason is not None
        assert "1 of 2 checks failed" in reason
        assert "orders_shipped: not shipped" in reason

    def test_all_skipped_returns_not_evaluated_sentinel(self, tmp_path: Path) -> None:
        artifacts_dir = _write_checks(tmp_path, _ALL_SKIP_CHECKS_PY)
        score, wire, reason = composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=_substrate(),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=artifacts_dir,
            check_executor=CheckRunner(),
            logger=_logger(),
        )
        assert score == -1.0
        assert [entry.status for entry in wire] == ["skipped", "skipped"]
        assert reason == "Custom checks: no check reached a verdict — all 2 skipped"

    def test_disabled_config_short_circuits_to_none_tuple(self) -> None:
        score, wire, reason = composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": False, "file": "checks.py"},
            substrate=_substrate(),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=None,
            check_executor=CheckRunner(),
            logger=_logger(),
        )
        assert (score, wire, reason) == (-1.0, [], None)

    def test_missing_artifacts_dir_emits_zero_sentinel_under_fail_on_error(self) -> None:
        score, wire, reason = composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={
                "enabled": True,
                "file": "checks.py",
                "interface_version": "1.0",
                "fail_on_error": True,
            },
            substrate=_substrate(),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=None,
            check_executor=CheckRunner(),
            logger=_logger(),
        )
        assert score == 0.0
        assert len(wire) == 1
        assert wire[0].check_name == "__executor__"
        assert wire[0].status == "error"
        assert "no artifacts_dir" in wire[0].message
        assert reason is not None
        assert reason.startswith("Custom checks: the suite failed to run —")
