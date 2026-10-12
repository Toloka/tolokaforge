"""``resolve_judge_trial_options``: a task's customization, and a run override, as options.

The one place a :class:`JudgeCustomization` becomes the :class:`JudgeTrialOptions` a
judge kind receives. Its defaults are a task with no ``customization`` block; its
tri-state fields collapse; the run-level ``grader.judge`` override wins field by field
where it is set and inherits where it is ``None``.
"""

from __future__ import annotations

import dataclasses

import pytest

from tolokaforge.core.grading.judge_kinds import JudgeTrialOptions, resolve_judge_trial_options
from tolokaforge.core.models.run_config import JudgeGraderConfig
from tolokaforge.runner.models import JudgeCustomization

pytestmark = pytest.mark.unit


def test_no_customization_is_the_library_default() -> None:
    assert resolve_judge_trial_options(None) == JudgeTrialOptions()
    assert JudgeTrialOptions() == JudgeTrialOptions(
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        judge_snippet_chars=200,
    )


def test_an_empty_block_is_the_library_default() -> None:
    assert resolve_judge_trial_options(JudgeCustomization()) == JudgeTrialOptions()


def test_every_customization_field_reaches_the_options() -> None:
    customization = JudgeCustomization(
        disable_knowledge_search=True,
        system_prompt="Grade strictly.",
        include_agent_system_prompt=False,
        judge_snippet_chars=None,
    )
    assert resolve_judge_trial_options(customization) == JudgeTrialOptions(
        disable_knowledge_search=True,
        custom_system_prompt="Grade strictly.",
        include_agent_system_prompt=False,
        judge_snippet_chars=None,
    )


def test_an_explicit_false_keeps_knowledge_search_and_true_includes_the_agent_prompt() -> None:
    customization = JudgeCustomization(
        disable_knowledge_search=False, include_agent_system_prompt=True
    )
    assert resolve_judge_trial_options(customization) == JudgeTrialOptions()


def test_a_set_override_field_wins_and_an_unset_one_inherits() -> None:
    customization = JudgeCustomization(
        disable_knowledge_search=True,
        system_prompt="Task voice.",
        include_agent_system_prompt=False,
        judge_snippet_chars=50,
    )
    override = JudgeGraderConfig(custom_system_prompt="Run voice.")
    assert resolve_judge_trial_options(customization, override=override) == JudgeTrialOptions(
        disable_knowledge_search=True,
        custom_system_prompt="Run voice.",
        include_agent_system_prompt=False,
        judge_snippet_chars=50,
    )
    full = JudgeGraderConfig(
        disable_knowledge_search=False,
        custom_system_prompt="Run voice.",
        include_agent_system_prompt=True,
    )
    resolved = resolve_judge_trial_options(customization, override=full)
    assert (resolved.disable_knowledge_search, resolved.include_agent_system_prompt) == (
        False,
        True,
    )
    assert resolved.judge_snippet_chars == 50, "the override carries no snippet length"


def test_an_override_applies_over_no_customization() -> None:
    override = JudgeGraderConfig(disable_knowledge_search=True)
    assert resolve_judge_trial_options(None, override=override) == JudgeTrialOptions(
        disable_knowledge_search=True
    )


def test_the_options_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        JudgeTrialOptions().judge_snippet_chars = None  # type: ignore[misc]
