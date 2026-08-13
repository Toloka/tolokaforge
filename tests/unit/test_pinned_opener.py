"""A task's ``initial_user_message`` is the pinned opener.

The conductor threads the field into the turn loop as the runner's seed
argument (``conductor.py`` → :meth:`TrialRunner.run`), so these tests build a
:class:`TaskConfig`, hand its opener to the runner the same way, and read the
trajectory the loop produced. The user simulator is a real scripted
:class:`UserSimulator` that counts its own dispatches — a mock would assert
that a call happened, not that the opening LLM turn was skipped.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm import GenerationResult, UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import classify_loop_error
from tolokaforge.core.models import (
    FirstUserMessageSource,
    Message,
    MessageRole,
    TaskConfig,
    Trajectory,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner

pytestmark = pytest.mark.unit

PINNED_OPENER = "  Hi, I want to return my DSLR camera.  "
GENERATED_OPENER = "I need help with my order"


class _CountingSimulator(UserSimulator):
    """Scripted simulator that records how often the loop dispatched it."""

    def __init__(self, reply_text: str) -> None:
        super().__init__(mode="scripted", scripted_flow=[{"user": reply_text}])
        self.dispatches = 0

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        self.dispatches += 1
        return super().reply(context, observation=observation)


def _make_agent_client() -> MagicMock:
    """Agent client whose every turn is plain text, so only turn 0 is interesting."""
    client = MagicMock()
    client.generate.return_value = GenerationResult(
        text="Let me look into that.",
        tool_calls=[],
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )
    client.classify_loop_error.side_effect = lambda exc: classify_loop_error(exc, ())
    return client


def _run_task(task: TaskConfig, simulator: _CountingSimulator) -> Trajectory:
    """Run one trial for *task*, seeding the runner the way the conductor does.

    One agent turn is all the budget allows, so the dispatch count is the
    bootstrap's — one under a generated opener, none under a pinned one — plus
    the single reply that turn earns.
    """
    runner = TrialRunner(
        task_id=task.task_id,
        trial_index=0,
        agent_client=_make_agent_client(),
        user_simulator=simulator,
        tool_executor=MagicMock(),
        tool_schemas=[],
        max_turns=1,
        turn_timeout_s=30,
        episode_timeout_s=600,
        interaction_mode=task.interaction_mode,
    )
    return runner.run("System", task.initial_user_message or "")


class TestPinnedOpener:
    """A declared opener is delivered verbatim; an undeclared one is generated."""

    def test_declared_opener_is_message_zero_verbatim(self) -> None:
        task = TaskConfig(
            task_id="pinned-opener-task",
            description="d",
            initial_user_message=PINNED_OPENER,
        )
        simulator = _CountingSimulator(GENERATED_OPENER)

        trajectory = _run_task(task, simulator)

        assert trajectory.messages[0].role is MessageRole.USER
        assert trajectory.messages[0].content == PINNED_OPENER
        assert simulator.dispatches == 1
        assert trajectory.first_user_message_source is FirstUserMessageSource.PINNED

    def test_unset_opener_is_generated_by_one_simulator_dispatch(self) -> None:
        task = TaskConfig(task_id="unset-opener-task", description="d")
        simulator = _CountingSimulator(GENERATED_OPENER)

        trajectory = _run_task(task, simulator)

        assert trajectory.messages[0].role is MessageRole.USER
        assert trajectory.messages[0].content == GENERATED_OPENER
        assert simulator.dispatches == 2
        assert trajectory.first_user_message_source is FirstUserMessageSource.SIMULATOR


class TestBlankOpenerRefused:
    """A declared-but-empty opener is a task-contract error, not a fallback."""

    @pytest.mark.parametrize("blank", ["", "   \n "], ids=["empty", "whitespace"])
    def test_blank_opener_refused_naming_both_authoring_surfaces(self, blank: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            TaskConfig(
                task_id="blank-opener-task",
                description="d",
                initial_user_message=blank,
            )

        message = str(excinfo.value)
        assert "initial_user_message" in message
        assert "blank-opener-task" in message
        assert "task.yaml" in message
        assert "get_task()" in message
