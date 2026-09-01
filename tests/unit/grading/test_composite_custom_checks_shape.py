"""``composite.grade_custom_checks`` — pure-``CheckResult`` return shape.

The composite returns ``list[CheckResult]``; the runner-side wire encoder
(:func:`project_check_result_to_runner_wire`) projects each into
``pb2.CustomCheckResult`` for the ``Grade.custom_checks`` field, and the
grader-side ``_build_grade`` reads the same ``CheckResult`` values
directly. These tests fence both surfaces at unit-tier cost:

- **Returned shape.** Five paths (happy / executor-error-in-result / no
  artifacts_dir / executor-raises / disabled) each assert the tuple's
  middle element is ``list[CheckResult]``, not a pb2 shape.
- **Encoder byte-lock.** The reserved ``__executor__`` sentinel encodes
  to the five pb2 field literals the pre-Stage-2 body of
  ``_executor_error_to_wire`` produced — checked as hard-coded literals so
  a future encoder edit trips the assertion.
- **Encoder details normalisation.** ``details`` payloads round-trip
  through ``json.dumps`` — an empty dict becomes empty-``details_json``,
  a primitive dict is JSON-encoded verbatim, a nested list is preserved,
  and a tuple is normalised to a list (the sole subtle Python-type
  divergence the pb2-drop plan calls out).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.check_runner import (
    _CHECK_EXECUTOR_ERROR_NAME,
    InMemoryCheckExecutor,
)
from tolokaforge.core.grading.checks_interface import (
    CheckResult,
    CheckResultSet,
    CheckStatus,
)
from tolokaforge.core.grading.composite import grade_custom_checks
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.runner.grading import project_check_result_to_runner_wire
from tolokaforge.runner.models import RunnerInitialStateConfig, TaskDescription

pytestmark = pytest.mark.unit


def _task_description() -> TaskDescription:
    return TaskDescription(
        task_id="shape:0",
        name="shape",
        category="test",
        description="composite return-shape lock",
        adapter_type="tau",
        system_prompt="",
        initial_state=RunnerInitialStateConfig(),
    )


def _substrate() -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state={},
    )


def _logger() -> logging.Logger:
    """Match the runner's dispatch: a plain :class:`logging.Logger`, not
    :class:`StructuredLogger` — the composite type-hints ``StructuredLogger``
    but the runner passes its module logger (``# type: ignore[arg-type]``);
    the executor-raised branch calls ``logger.exception(...)`` which only
    exists on the Python logger.
    """
    return logging.getLogger("test-composite-custom-checks-shape")


def _write_empty_checks(tmp_path: Path) -> Path:
    (tmp_path / "checks.py").write_text("", encoding="utf-8")
    return tmp_path


def _run(
    *,
    executor: InMemoryCheckExecutor,
    tmp_path: Path | None,
    fail_on_error: bool = False,
) -> tuple[float, list[CheckResult], str | None]:
    return grade_custom_checks(
        trial_id="shape:0",
        config={
            "enabled": True,
            "file": "checks.py",
            "interface_version": "1.0",
            "fail_on_error": fail_on_error,
        },
        substrate=_substrate(),
        llm_messages=[],
        task_description=_task_description(),
        artifacts_dir=tmp_path,
        check_executor=executor,
        logger=_logger(),
    )


def test_happy_path_returns_list_of_check_results(tmp_path: Path) -> None:
    happy = CheckResult(
        check_name="a",
        status=CheckStatus.PASSED,
        score=1.0,
        message="ok",
        details={"foo": "bar"},
    )
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[happy]))
    score, results, reason = _run(executor=executor, tmp_path=_write_empty_checks(tmp_path))

    assert score == pytest.approx(1.0)
    assert len(results) == 1
    assert isinstance(results[0], CheckResult)
    assert results[0].check_name == "a"
    assert results[0].status == CheckStatus.PASSED
    assert results[0].details == {"foo": "bar"}
    assert reason is not None and reason != ""


def test_executor_error_captured_in_result_error_appends_sentinel(tmp_path: Path) -> None:
    failed = CheckResult(
        check_name="a",
        status=CheckStatus.FAILED,
        score=0.0,
        message="explicit failure",
        details={},
    )
    executor = InMemoryCheckExecutor(
        result_set=CheckResultSet(results=[failed], error="module import failed"),
    )
    _score, results, _reason = _run(
        executor=executor,
        tmp_path=_write_empty_checks(tmp_path),
        fail_on_error=True,
    )

    assert results[0].check_name == "a"
    assert results[-1].check_name == _CHECK_EXECUTOR_ERROR_NAME
    assert results[-1].status == CheckStatus.ERROR
    assert results[-1].score == 0.0
    assert results[-1].message == "module import failed"
    assert results[-1].details == {}


def test_missing_artifacts_dir_returns_sentinel_check_result() -> None:
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[]))
    score, results, reason = _run(executor=executor, tmp_path=None, fail_on_error=True)

    assert score == 0.0
    assert len(results) == 1
    assert isinstance(results[0], CheckResult)
    assert results[0].check_name == _CHECK_EXECUTOR_ERROR_NAME
    assert results[0].status == CheckStatus.ERROR
    assert "no artifacts_dir" in results[0].message
    assert reason is not None


def test_executor_raises_returns_sentinel_check_result(tmp_path: Path) -> None:
    executor = InMemoryCheckExecutor(raise_on_run=RuntimeError("boom"))
    score, results, reason = _run(
        executor=executor,
        tmp_path=_write_empty_checks(tmp_path),
        fail_on_error=True,
    )

    assert score == 0.0
    assert len(results) == 1
    assert results[0].check_name == _CHECK_EXECUTOR_ERROR_NAME
    assert results[0].status == CheckStatus.ERROR
    assert results[0].message == "boom"
    assert reason is not None


def test_custom_checks_disabled_returns_empty_result_tuple() -> None:
    executor = InMemoryCheckExecutor(result_set=CheckResultSet(results=[]))
    result = grade_custom_checks(
        trial_id="shape:0",
        config={"enabled": False, "file": "checks.py"},
        substrate=_substrate(),
        llm_messages=[],
        task_description=_task_description(),
        artifacts_dir=None,
        check_executor=executor,
        logger=_logger(),
    )

    assert result == (-1.0, [], None)


def test_executor_error_sentinel_encoder_matches_hard_coded_wire_literals() -> None:
    """The reserved ``__executor__`` sentinel encodes to five pb2 field literals.

    Hard-coded — not compared against any helper the pb2-drop stage removes.
    Locks the encoder's byte-identical projection at unit-tier cost, before
    the 10-pack canonical parity gate has a chance to run.
    """
    sentinel = CheckResult(
        check_name=_CHECK_EXECUTOR_ERROR_NAME,
        status=CheckStatus.ERROR,
        score=0.0,
        message="module import failed",
        details={},
    )

    wire = project_check_result_to_runner_wire(sentinel)

    assert wire.check_name == "__executor__"
    assert wire.status == "error"
    assert wire.score == 0.0
    assert wire.message == "module import failed"
    assert wire.details_json == ""


@pytest.mark.parametrize(
    ("details", "expected_details_json"),
    [
        ({}, ""),
        ({"k": "v"}, '{"k": "v"}'),
        ({"k": [1, 2, 3]}, '{"k": [1, 2, 3]}'),
        ({"k": (1, 2)}, '{"k": [1, 2]}'),
    ],
    ids=[
        "empty_dict_becomes_empty_string",
        "primitive_dict",
        "nested_list",
        "tuple_normalises_to_list",
    ],
)
def test_details_encoder_normalisation_shapes(
    details: dict[str, Any], expected_details_json: str
) -> None:
    """``project_check_result_to_runner_wire`` details JSON contract.

    Locks: empty-dict → empty-``details_json``, primitive round-trip,
    list round-trip, and — the sole subtle Python-type divergence the
    pb2-drop plan calls out — tuple values normalise to lists via
    ``json.dumps``. Every pre-Stage-2 fixture is primitive-safe; this
    test names the tuple contract at unit-tier so a future fixture
    author sees the constraint in the failing test rather than in a
    mystery 10-pack red.
    """
    result = CheckResult(
        check_name="detail",
        status=CheckStatus.PASSED,
        score=1.0,
        message="ok",
        details=details,
    )

    wire = project_check_result_to_runner_wire(result)

    assert wire.details_json == expected_details_json
