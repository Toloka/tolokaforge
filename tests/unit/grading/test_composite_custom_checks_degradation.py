"""``composite.grade_custom_checks`` — degrade-to-empty on DB failure.

The composite catches any failure fetching the trial's final DB state and
falls through with ``final_env_state = {}`` — DB Service unreachable,
trial never registered, connection reset mid-grade. Any substrate raising
this class of failure inherits the same fallback.

:class:`SubstrateUnreachableError` is the one exception: the source is
unreachable, the trial's state can no longer be produced, and the grade
must fail loud rather than book an empty-state verdict against phantom
evidence. That path is locked separately below.

Three behaviours the composite must uphold, all locked here:

- ``final_env_state`` reaching :func:`build_check_context` is the empty dict
  when :meth:`substrate.final_state` raises any non-substrate-unreachable
  error (proved by asserting ``ctx.final_state.data == {}`` on the
  executor's captured call).
- The audit signal is the log line downstream tooling greps for —
  ``"final DB state fetch failed (<exc>); grading against empty state"``.
  The wording is a stability contract; downstream audit and alerting
  consume it verbatim.
- :class:`SubstrateUnreachableError` propagates unchanged so the caller
  books the trial as ungradeable instead of returning a valid-looking
  Grade against ``{}``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading import composite
from tolokaforge.core.grading.check_runner import InMemoryCheckExecutor
from tolokaforge.core.grading.checks_interface import CheckResultSet
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SubstrateUnreachableError,
)
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.runner.models import RunnerInitialStateConfig, TaskDescription

pytestmark = pytest.mark.unit


def _task_description() -> TaskDescription:
    return TaskDescription(
        task_id="reconcile:0",
        name="reconcile",
        category="test",
        description="Ledger reconciliation.",
        adapter_type="tau",
        system_prompt="",
        initial_state=RunnerInitialStateConfig(),
    )


def _raising_substrate(exc: Exception) -> InProcessGradingSubstrate:
    """Substrate whose ``final_state`` factory raises ``exc``.

    ``final_state_factory`` is invoked once by the composite and never re-tried.
    """

    def _boom() -> dict[str, Any]:
        raise exc

    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state_factory=_boom,
    )


def test_final_state_read_failure_degrades_to_empty_ctx(tmp_path: Path) -> None:
    """The executor is dispatched with ``ctx.final_state.data == {}``.

    Locks the shipped `degrade-to-empty` semantics on the composite: any
    exception from :meth:`substrate.final_state` (DB Service unreachable, DB
    trial never registered, network reset) leaves ``final_env_state = {}``
    and the check still runs against an honest empty state rather than
    crashing the grade path. The :class:`InMemoryCheckExecutor` records the
    dispatched ``ctx.final_state.data.keys()`` on ``call_log.runs`` — an
    empty tuple there is proof the degradation reached the ``CheckContext``.
    """
    (tmp_path / "checks.py").write_text("", encoding="utf-8")
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[]))

    composite.grade_custom_checks(
        trial_id="reconcile:0",
        config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
        substrate=_raising_substrate(ConnectionError("db-service down")),
        llm_messages=[],
        task_description=_task_description(),
        artifacts_dir=tmp_path,
        check_executor=executor,
        logger=StructuredLogger(name="test-composite-custom-checks-degradation"),
    )

    assert len(executor.call_log.runs) == 1
    assert executor.call_log.runs[0]["final_state_keys"] == ()


def test_final_state_read_failure_logs_shipped_wording(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The emitted log record must match the shipped wording verbatim.

    Downstream tooling greps for the sentence ``"final DB state fetch failed
    (<exc>); grading against empty state"`` — the composite renders it
    byte-identical to the runner's shipped ``service.py`` message.
    """
    (tmp_path / "checks.py").write_text("", encoding="utf-8")
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[]))

    with caplog.at_level(logging.WARNING):
        composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=_raising_substrate(ConnectionError("db-service down")),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=tmp_path,
            check_executor=executor,
            logger=StructuredLogger(name="test-composite-custom-checks-degradation-log"),
        )

    assert any(
        "final DB state fetch failed (db-service down); grading against empty state"
        in record.getMessage()
        for record in caplog.records
    )
    assert any("reconcile:0" in record.getMessage() for record in caplog.records)


def test_substrate_unreachable_propagates(tmp_path: Path) -> None:
    """A ``SubstrateUnreachableError`` from ``final_state`` must NOT degrade.

    The substrate contract makes ``SubstrateUnreachableError`` a fail-loud
    signal: the source is gone, the trial's state can no longer be
    produced, and any Grade the composite could return would be against
    phantom evidence. Locking here catches a future refactor that widens
    the broad-except in :func:`grade_custom_checks` back over
    :class:`SubstrateUnreachableError`.
    """
    (tmp_path / "checks.py").write_text("", encoding="utf-8")
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[]))

    with pytest.raises(SubstrateUnreachableError):
        composite.grade_custom_checks(
            trial_id="reconcile:0",
            config={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
            substrate=_raising_substrate(
                SubstrateUnreachableError("grader lost the runner mid-grade")
            ),
            llm_messages=[],
            task_description=_task_description(),
            artifacts_dir=tmp_path,
            check_executor=executor,
            logger=StructuredLogger(name="test-composite-custom-checks-fail-loud"),
        )

    assert executor.call_log.runs == []
