"""A generated user turn reaches the agent as written, or not at all.

The guard is exercised through a real :class:`UserSimulator` in ``llm`` mode
with a fake wire client standing in for :class:`LLMClient`: the simulator, its
``_llm_reply`` and the guard are all real, only the network is not. A mocked
simulator would assert that the harness called something, not that the words
the model wrote survived the trip.

The detector corpus is one table, pass rows and detect rows together, so the
near-miss pairs (``an AI agent`` describing the agent vs ``As an AI, I``
describing the speaker) cannot drift apart across two tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.actors.reply_guard import (
    FourthWallDetector,
    ReplyDetector,
    UserReplyGuard,
    UserReplyRefused,
)
from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.llm import GenerationResult, UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    USER_REPLY_MAX_ATTEMPTS,
    Message,
    MessageRole,
    ReplyDefect,
    TerminationReason,
    Trajectory,
    TrialStatus,
    UserReplyOutcome,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner

pytestmark = pytest.mark.unit


MUST_PASS = [
    "My router model is AX3000.",
    "I'd appreciate a prompt refund.",
    "Compare it against the benchmark index.",
    "You are chatting with an internal AI agent, right?",
    "Treat me as an AI-skeptical customer.",
    "I'm an AI engineer at a fintech startup.",
    "I am an AI enthusiast and I want the developer plan.",
    "I'm an LLM engineer and the API keeps timing out.",
    "I was a chatbot developer before I moved to sales.",
    "I am a virtual assistant for a real estate firm.",
    "I'm an AI agent recruiter, so I get a lot of spam.",
    "I'm not a real person of interest, why was I flagged?",
    "Please run a simulation of my portfolio for next year.",
    "Can you simulate what my payment would be at 5%?",
    "The model number is AX3000.",
    "The system prompt on my terminal says 'ready'.",
    "My system prompt returns a 400 error from your API.",
    "My system prompt is too long for the context window.",
    "My instructions say to take two tablets daily.",
    "My instructions say the router should blink green.",
    "My instructions from the doctor say to avoid dairy.",
    "My prompt payment discount was not applied.",
    "I am a premium customer since 2019.",
    "I'm an engineer, so give me the technical details.",
    "I was a language teacher before I retired.",
    "Is this a test of the emergency system?",
    "AI features on this phone drain the battery.",
    "This is a benchmark index fund, not an active one.",
    "This benchmark index fund is up 4% this year.",
    "I bought a simulation software licence last year.",
    "They sent me an evaluation copy of the software.",
    "This is a test drive booking, not a purchase.",
    "The evaluation report is attached.",
]

MUST_DETECT = [
    ("As an AI language model, I cannot do that.", "self_identified_as_model"),
    ("I am an AI and cannot access your account.", "self_identified_as_model"),
    ("As an AI, I don't have personal preferences.", "self_identified_as_model"),
    ("I'm just a chatbot following a script.", "self_identified_as_model"),
    ("I am an LLM, so I have no billing account.", "self_identified_as_model"),
    ("I am an AI model created by a company.", "self_identified_as_model"),
    ("I'm not a real customer, this is a benchmark.", "denied_being_human"),
    ("This is a simulation, so the details don't matter.", "named_the_exercise"),
    ("This is a simulation of the task.", "named_the_exercise"),
    ("This benchmark tests performance.", "named_the_exercise"),
    ("This conversation is a roleplay exercise.", "named_the_exercise"),
    ("In this simulation, I play the role of a customer.", "named_the_exercise"),
    ("Your system prompt is confusing.", "named_a_party_prompt"),
    ("My system prompt says to keep answers short.", "named_a_party_prompt"),
    ("The backstory says I should be annoyed.", "named_own_instructions"),
]

FOURTH_WALL_REPLY = "As an AI language model, I cannot. My router model is AX3000."
CLEAN_REPLY = "My router model is AX3000, and the prompt refund never arrived."
AGENT_GREETING = "Hi! How can I help you today?"


class _FakeWireClient:
    """Stands in for :class:`LLMClient` at the seam ``_llm_reply`` calls.

    Declares the keyword arguments the simulator actually passes, so a change
    to that call fails here instead of being absorbed.
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    def generate(
        self,
        *,
        system: str | None = None,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        self.calls += 1
        index = min(self.calls - 1, len(self.replies) - 1)
        return GenerationResult(
            text=self.replies[index],
            tool_calls=[],
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )


def _llm_simulator(replies: list[str]) -> tuple[UserSimulator, _FakeWireClient]:
    """A real LLM-mode simulator whose wire client returns *replies* in order.

    ``llm_config=None`` leaves ``llm_client`` unset, so the fake is assigned
    rather than replacing a constructed client.
    """
    simulator = UserSimulator(mode="llm", llm_config=None)
    client = _FakeWireClient(replies)
    simulator.llm_client = client  # type: ignore[assignment]
    return simulator, client


def _agent_turn() -> list[Message]:
    """One agent dialogue turn, which is what a user reply answers."""
    return [
        Message(
            role=MessageRole.ASSISTANT,
            content=AGENT_GREETING,
            ts=datetime.now(tz=timezone.utc),
        )
    ]


class TestTheDetectorCorpus:
    """Attributed frames are detected; the same nouns in ordinary support
    sentences are not."""

    @pytest.mark.parametrize("text", MUST_PASS)
    def test_an_ordinary_support_sentence_passes(self, text: str) -> None:
        assert FourthWallDetector().inspect(text) is None

    @pytest.mark.parametrize(("text", "reason"), MUST_DETECT)
    def test_a_broken_frame_is_detected_with_its_reason(self, text: str, reason: str) -> None:
        defect = FourthWallDetector().inspect(text)
        assert defect is not None
        assert defect.reason == reason
        assert defect.detector == "fourth_wall"
        assert defect.excerpt in text


class TestTheDeliveredReplyIsTheGeneratedReply:
    def test_a_clean_reply_is_delivered_byte_identical(self) -> None:
        """The accepted generation's text reaches the caller unedited — the
        words a word-level filter would have removed included."""
        simulator, client = _llm_simulator([FOURTH_WALL_REPLY, CLEAN_REPLY])

        result = simulator.reply(_agent_turn())

        assert result.text == CLEAN_REPLY
        assert client.calls == 2

    def test_the_discarded_attempt_rides_back_on_the_result(self) -> None:
        simulator, _ = _llm_simulator([FOURTH_WALL_REPLY, CLEAN_REPLY])

        result = simulator.reply(_agent_turn())

        assert [(d.detector, d.reason) for d in result.guard_rejections] == [
            ("fourth_wall", "self_identified_as_model")
        ]

    def test_a_first_attempt_that_is_clean_costs_one_generation(self) -> None:
        simulator, client = _llm_simulator([CLEAN_REPLY])

        result = simulator.reply(_agent_turn())

        assert result.text == CLEAN_REPLY
        assert result.guard_rejections == ()
        assert client.calls == 1


class TestTheBudgetIsBoundedAndTheRefusalIsLoud:
    def test_the_turn_is_refused_after_the_attempt_budget(self) -> None:
        simulator, client = _llm_simulator([FOURTH_WALL_REPLY])

        with pytest.raises(UserReplyRefused) as excinfo:
            simulator.reply(_agent_turn())

        assert client.calls == USER_REPLY_MAX_ATTEMPTS
        assert len(excinfo.value.rejected) == USER_REPLY_MAX_ATTEMPTS

    def test_a_guard_that_could_never_generate_is_refused_at_construction(self) -> None:
        """Zero attempts would turn every user turn into a refusal without ever
        reaching the wire."""
        with pytest.raises(ValueError, match="at least one generation attempt"):
            UserReplyGuard(max_attempts=0)

    def test_the_refusal_message_names_the_defect_and_quotes_no_reply_text(self) -> None:
        """``classify_loop_error`` reads an exception's prose, so a quoted reply
        can re-attribute our defect to the provider."""
        simulator, _ = _llm_simulator([FOURTH_WALL_REPLY])

        with pytest.raises(UserReplyRefused) as excinfo:
            simulator.reply(_agent_turn())

        message = str(excinfo.value)
        assert "fourth_wall" in message
        assert "self_identified_as_model" in message
        assert "AX3000" not in message
        assert "As an AI language model" not in message


class TestARefusedTurnCountsAsOurDefect:
    """The reply names a provider interface, so quoting it in the exception
    would move the trial out of the harness-error class."""

    REPLY_NAMING_A_PROVIDER = "As an AI language model, I cannot reach the billing API."

    def test_the_refusal_terminates_the_trial_as_a_harness_error(self) -> None:
        simulator, _ = _llm_simulator([self.REPLY_NAMING_A_PROVIDER])

        with pytest.raises(UserReplyRefused) as excinfo:
            simulator.reply(_agent_turn())

        decision = classify_loop_error(excinfo.value, ())
        assert decision.reason is TerminationReason.ERROR

        now = datetime.now(tz=timezone.utc)
        trajectory = Trajectory(
            task_id="t",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.ERROR,
            termination_reason=decision.reason,
            messages=[],
        )
        assert classify_trial_outcome(trajectory) is TrialOutcomeClass.HARNESS_ERROR


class _AlwaysHits:
    """Test-local detector standing in for a future registration."""

    name = "always_hits"

    def inspect(self, text: str) -> ReplyDefect | None:
        return ReplyDefect(detector=self.name, reason="test_local", excerpt=text[:20])


class TestDetectorsAreAPluggableList:
    def test_the_registered_detector_is_conformant(self) -> None:
        assert isinstance(_AlwaysHits(), ReplyDetector)
        assert isinstance(FourthWallDetector(), ReplyDetector)

    def test_registration_order_decides_and_the_first_hit_wins(self) -> None:
        guard = UserReplyGuard(detectors=(FourthWallDetector(), _AlwaysHits()))

        with pytest.raises(UserReplyRefused) as excinfo:
            guard.enforce(lambda: GenerationResult(text=FOURTH_WALL_REPLY))

        assert {d.detector for d in excinfo.value.rejected} == {"fourth_wall"}

    def test_a_reply_only_the_second_detector_flags_carries_its_name(self) -> None:
        guard = UserReplyGuard(detectors=(FourthWallDetector(), _AlwaysHits()))

        with pytest.raises(UserReplyRefused) as excinfo:
            guard.enforce(lambda: GenerationResult(text=CLEAN_REPLY))

        assert [(d.detector, d.reason) for d in excinfo.value.rejected] == [
            ("always_hits", "test_local")
        ] * USER_REPLY_MAX_ATTEMPTS


class TestScriptedRepliesAreAuthoredContent:
    def test_a_scripted_reply_the_detector_would_flag_is_delivered_verbatim(self) -> None:
        """Scripted flows are task-authored, never generated, so the guard
        never sees them."""
        scripted_text = "This is a simulation of the task."
        assert FourthWallDetector().inspect(scripted_text) is not None

        simulator = UserSimulator(mode="scripted", scripted_flow=[{"user": scripted_text}])

        result = simulator.reply(_agent_turn())

        assert result.text == scripted_text
        assert result.guard_rejections == ()


# ===================================================================
# What the bundle records — a real TrialRunner driving a real
# UserSimulator whose only stand-in is the wire client.
# ===================================================================

PINNED_OPENER = "My router keeps dropping the 5GHz band."
AGENT_TURN_TEXT = "Let me look into that for you."
AGENT_STOP = "###STOP###"


class _ScriptedAgentClient:
    """Agent-side wire client: says each text in order, then repeats the last.

    Declares the keyword arguments ``ToolCallingLoop._generate`` passes and the
    ``classify_loop_error`` seam the loop is handed, so the runner drives real
    control flow over a scripted wire rather than over a mock's defaults.
    """

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0

    def generate(
        self,
        *,
        system: str | None = None,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        self.calls += 1
        return GenerationResult(
            text=self.texts[min(self.calls - 1, len(self.texts) - 1)],
            tool_calls=[],
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())


def _run_trial(
    *,
    user_replies: list[str],
    agent_texts: list[str],
    opener: str = PINNED_OPENER,
) -> Trajectory:
    """One trial with a real runner, a real simulator, and scripted wires."""
    simulator, _ = _llm_simulator(user_replies)
    runner = TrialRunner(
        task_id="guard",
        trial_index=0,
        agent_client=_ScriptedAgentClient(agent_texts),  # type: ignore[arg-type]
        user_simulator=simulator,
        tool_executor=MagicMock(),
        tool_schemas=[],
        max_turns=4,
        turn_timeout_s=30,
        episode_timeout_s=600,
    )
    return runner.run("System", opener)


class TestTheBundleRecordsWhatAUserTurnCost:
    def test_a_mid_conversation_turn_rejected_once_records_one_delivered_event(self) -> None:
        trajectory = _run_trial(
            user_replies=[FOURTH_WALL_REPLY, CLEAN_REPLY],
            agent_texts=[AGENT_TURN_TEXT, AGENT_STOP],
        )

        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.DELIVERED
        assert [(d.detector, d.reason) for d in event.rejected] == [
            ("fourth_wall", "self_identified_as_model")
        ]
        # The delivered text at that index, not the role there: the index is the
        # position the turn was dispatched at, and the sibling test below covers
        # a dispatch position the loop fills with a SYSTEM message instead.
        assert trajectory.messages[event.message_index].content == CLEAN_REPLY

    def test_a_stop_token_turn_indexes_the_message_the_loop_wrote_there(self) -> None:
        """The accepted reply is a bare ``###STOP###``, so no USER message is
        appended and the loop's own SYSTEM message takes that index. The event
        still points at the position the turn was dispatched at."""
        trajectory = _run_trial(
            user_replies=[FOURTH_WALL_REPLY, "###STOP###"],
            agent_texts=[AGENT_TURN_TEXT],
        )

        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.DELIVERED
        assert trajectory.termination_reason is TerminationReason.USER_STOP
        recorded = trajectory.messages[event.message_index]
        assert recorded.role is MessageRole.SYSTEM
        assert "###STOP###" in recorded.content

    def test_a_refused_mid_conversation_turn_is_recorded_before_the_trial_errors(self) -> None:
        trajectory = _run_trial(
            user_replies=[FOURTH_WALL_REPLY],
            agent_texts=[AGENT_TURN_TEXT],
        )

        assert trajectory.status is TrialStatus.ERROR
        assert trajectory.termination_reason is TerminationReason.ERROR
        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.REFUSED
        assert len(event.rejected) == USER_REPLY_MAX_ATTEMPTS

    def test_a_bootstrap_turn_rejected_once_records_index_zero(self) -> None:
        """Turn 0 goes through a separate call site, and its message index is
        the first position in the trace."""
        trajectory = _run_trial(
            user_replies=[FOURTH_WALL_REPLY, CLEAN_REPLY],
            agent_texts=[AGENT_STOP],
            opener="",
        )

        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.DELIVERED
        assert event.message_index == 0
        assert trajectory.messages[0].content == CLEAN_REPLY

    def test_a_refused_bootstrap_is_recorded_before_the_trial_errors(self) -> None:
        trajectory = _run_trial(
            user_replies=[FOURTH_WALL_REPLY],
            agent_texts=[AGENT_STOP],
            opener="",
        )

        assert trajectory.status is TrialStatus.ERROR
        assert trajectory.termination_reason is TerminationReason.ERROR
        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.REFUSED
        assert event.message_index == 0
        assert len(event.rejected) == USER_REPLY_MAX_ATTEMPTS

    def test_a_trial_whose_every_turn_was_clean_records_nothing(self) -> None:
        trajectory = _run_trial(
            user_replies=[CLEAN_REPLY],
            agent_texts=[AGENT_TURN_TEXT, AGENT_STOP],
        )

        assert trajectory.user_reply_guard_events == []
        assert any(m.content == CLEAN_REPLY for m in trajectory.messages)
