"""A trial's judge options: its effective customization, after the run-level layer.

:func:`resolve_judge_trial_options` turns the effective customization into the
:class:`JudgeTrialOptions` a judge kind receives; its defaults are a task with no
``customization`` block. The ``judge_only`` grader layers the run-level ``grader.judge``
block over the task's customization first, key by key, and records the result on the
grade it writes.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.unit.grading.test_judge import ScriptedClient
from tolokaforge.core.grading.judge_kinds import JudgeTrialOptions, resolve_judge_trial_options
from tolokaforge.core.grading.judge_only_helpers import run_judge_only_for_trajectory
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Message, MessageRole, ModelConfig, Trajectory, TrialStatus
from tolokaforge.core.models.run_config import GraderConfig
from tolokaforge.runner.models import JudgeCustomization, LLMJudgeConfig

pytestmark = pytest.mark.unit

_RUBRIC = {"criteria": [{"id": "greets", "description": "The agent greets", "kind": "binary"}]}
_SUBMIT_REPORT = [
    [
        (
            "submit_report",
            {
                "reasons": "Greeted.",
                "greets": True,
                "greets_justification": "It says hello. VERDICT: MET",
            },
        )
    ]
]


def test_no_customization_is_the_library_default() -> None:
    assert resolve_judge_trial_options(None, agent_tools=()) == JudgeTrialOptions()
    assert JudgeTrialOptions().customization == JudgeCustomization()


def test_the_options_carry_the_customization_they_were_resolved_from() -> None:
    customization = JudgeCustomization(
        system_prompt="Grade strictly.",
        evidence={
            "knowledge_search": {"snippet_chars": None},
            "agent_policy": False,
        },
    )
    assert resolve_judge_trial_options(customization, agent_tools=()).customization is (
        customization
    )


def _judge_only_customization(
    task_customization: dict[str, Any] | None, run_judge: dict[str, Any]
) -> JudgeCustomization | None:
    """The customization a ``judge_only`` grade records, for a task and a ``grader.judge``."""
    now = datetime.now(UTC)
    grade = run_judge_only_for_trajectory(
        trial_id="t",
        llm_judge_config=LLMJudgeConfig.model_validate(
            {"rubric": _RUBRIC, "customization": task_customization}
        ),
        judge_model_config=ModelConfig(provider="openai", name="gpt-4o-mini"),
        trajectory=Trajectory(
            task_id="t",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.COMPLETED,
            messages=[Message(role=MessageRole.ASSISTANT, content="Hello.")],
        ),
        agent_system_prompt="Be polite.",
        agent_tools=[],
        override=GraderConfig.model_validate({"judge": run_judge}).judge,
        llm_client=ScriptedClient(_SUBMIT_REPORT),
        logger=get_logger("test"),
    )
    assert grade is not None and grade.judge_inputs is not None
    return grade.judge_inputs.customization


@pytest.mark.parametrize(
    ("task_customization", "run_judge", "recorded"),
    [
        (
            {"system_prompt": "Task voice.", "evidence": {"agent_policy": False}},
            {"system_prompt": "Run voice."},
            JudgeCustomization(system_prompt="Run voice.", evidence={"agent_policy": False}),
        ),
        (
            {
                "system_prompt": "Task voice.",
                "evidence": {"knowledge_search": {"snippet_chars": 50}},
            },
            {"system_prompt": None},
            JudgeCustomization(evidence={"knowledge_search": {"snippet_chars": 50}}),
        ),
        (
            {"evidence": {"knowledge_search": {"snippet_chars": 50}}},
            {"evidence": {"knowledge_search": True}},
            JudgeCustomization(),
        ),
        (
            {"evidence": {"db": False, "tool_results": True}},
            {"evidence": {"db": True}},
            JudgeCustomization(evidence={"tool_results": True}),
        ),
        (
            None,
            {"evidence": {"knowledge_search": False}},
            JudgeCustomization(evidence={"knowledge_search": False}),
        ),
        (
            {"evidence": {"tool_results": {"max_chars": 9}}},
            {},
            JudgeCustomization(evidence={"tool_results": {"max_chars": 9}}),
        ),
    ],
    ids=[
        "one-key-inherits",
        "null-resets-prompt",
        "true-resets-snippet",
        "one-part-inherits",
        "over-none",
        "empty",
    ],
)
def test_the_run_level_layer_wins_on_the_keys_it_sets_and_the_grade_records_the_result(
    task_customization: dict[str, Any] | None,
    run_judge: dict[str, Any],
    recorded: JudgeCustomization,
) -> None:
    assert _judge_only_customization(task_customization, run_judge) == recorded


def test_the_options_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        JudgeTrialOptions().customization = JudgeCustomization()  # type: ignore[misc]
