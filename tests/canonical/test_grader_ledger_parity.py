"""Seam-level ledger parity locks for :class:`GraderCompositeDispatch`.

Two canonical locks that fail loud on future removal of the grader-side
recording sites for the ``llm_judge`` and ``custom_checks`` blocks. The
byte-parity pack under ``grader_parity_baselines/custom_checks_disabled_ledger_skip/``
locks the wire-serialised Grade end-to-end; these locks catch drift at the
Python-level helper granularity where an implementer touches individual sites.

Both drive the private ``_grade_*_block`` helper directly against a stub
substrate and assert the returned ledger-accounting dict carries the expected
:class:`KeyAccountingRecord` for the skip branch. Direct-helper calls stay
below :meth:`GraderCompositeDispatch._run_composite`'s fold so a populated
``llm_judge`` block with no messages — a shape the fold refuses because
``component_requested`` treats a populated ``llm_judge`` as requested — still
exercises the recording site the audit reads.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.grade_components import CompositeGradeComponents
from tolokaforge.core.models import KeyAccounting, ModelConfig
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.runner.grading_ledger import (
    CUSTOM_CHECKS_DISABLED_SKIP,
    CUSTOM_CHECKS_KEY,
    LLM_JUDGE_KEY,
    NO_JUDGE_MESSAGES_SKIP,
)
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    TaskDescription,
)

pytestmark = pytest.mark.canonical


_TRIAL_ID = "seam-lock:0"
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)
_STATE: dict[str, list[dict[str, Any]]] = {"users": [{"id": "u1", "name": "Alice"}]}


class _StubSubstrate:
    """Constant-snapshot substrate reaching every accessor without a network hop."""

    def initial_state(self) -> dict[str, Any]:
        return dict(_STATE)

    def final_state(self) -> dict[str, Any]:
        return dict(_STATE)

    def final_state_stable(self) -> dict[str, Any]:
        return dict(_STATE)

    def filesystem_root(self):  # type: ignore[no-untyped-def]
        return None

    def filesystem_state(self) -> dict[str, str] | None:
        return None

    def db_reader(self) -> Any:
        reader = MagicMock()
        reader.get_state = lambda tables=None: dict(_STATE)
        reader.query = lambda jp: {"results": []}
        return reader

    def knowledge_search(self) -> Any:
        return None

    def close(self) -> None:
        return None


def _task_description() -> TaskDescription:
    return TaskDescription.model_validate(
        {
            "task_id": "seam-lock-task",
            "name": "ledger-parity seam-lock fixture",
            "category": "test",
            "description": "seam-lock fixture",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_STATE).model_dump(),
            "agent_tools": [],
            "user_tools": [],
            "grading": {},
        }
    )


def test_llm_judge_no_messages_records_skip() -> None:
    """A populated ``llm_judge`` block with an empty transcript files
    :data:`NO_JUDGE_MESSAGES_SKIP` under :data:`LLM_JUDGE_KEY` so the audit
    reads a ``SKIPPED`` record instead of an unaccounted-key error.

    Locks the grader-side recording site :meth:`GraderCompositeDispatch._grade_llm_judge_block`
    that :meth:`_run_composite` merges into ``accounted_keys`` before the audit
    fires. A refactor that silently strips this site would surface as this
    helper returning an empty dict, and the audit would then raise
    :class:`GradingFailedError` on the unaccounted ``llm_judge`` key.
    """
    dispatch = GraderCompositeDispatch(logger=MagicMock())
    llm_judge = LLMJudgeConfig(
        rubric=Rubric(
            criteria=[Criterion(id="ok", description="Task completed", kind="binary", weight=1.0)]
        )
    )

    _, judge_status, judge_gate_failed, accounted = dispatch._grade_llm_judge_block(
        trial_id=_TRIAL_ID,
        llm_judge_config=llm_judge,
        judge_model_config=_JUDGE_MODEL,
        llm_messages=[],
        substrate=_StubSubstrate(),  # type: ignore[arg-type]
        initial_state_schemas=[],
        id_fields={},
        unstable_fields=set(),
        components=CompositeGradeComponents(),
    )

    assert accounted == {LLM_JUDGE_KEY: NO_JUDGE_MESSAGES_SKIP}
    assert accounted[LLM_JUDGE_KEY].outcome is KeyAccounting.SKIPPED
    assert judge_status.name == "UNSPECIFIED"
    assert judge_gate_failed is False


def test_custom_checks_disabled_records_skip() -> None:
    """A ``custom_checks: {enabled: false}`` block files
    :data:`CUSTOM_CHECKS_DISABLED_SKIP` under :data:`CUSTOM_CHECKS_KEY` so
    the audit reads a ``SKIPPED`` record.

    Locks the grader-side recording site :meth:`GraderCompositeDispatch._grade_custom_checks_block`
    that :meth:`_run_composite` merges into ``accounted_keys`` before the audit
    fires. A refactor that silently strips this site would surface as this
    helper returning an empty dict, and the audit would then raise
    :class:`GradingFailedError` on the unaccounted ``custom_checks`` key.
    """
    dispatch = GraderCompositeDispatch(logger=MagicMock())
    grading_config = RunnerGradingConfig(
        weights={},
        custom_checks={"enabled": False},
    )
    task = _task_description()

    _, _, accounted = dispatch._grade_custom_checks_block(
        trial_id=_TRIAL_ID,
        grading_config=grading_config,
        task_description=task,
        llm_messages=[],
        substrate=_StubSubstrate(),  # type: ignore[arg-type]
        artifacts_dir=None,
        components=CompositeGradeComponents(),
    )

    assert accounted == {CUSTOM_CHECKS_KEY: CUSTOM_CHECKS_DISABLED_SKIP}
    assert accounted[CUSTOM_CHECKS_KEY].outcome is KeyAccounting.SKIPPED
