"""A generated user turn reaches the agent as written, or not at all.

The guard is exercised through a real :class:`UserSimulator` in ``llm`` mode
with a fake wire client standing in for :class:`LLMClient`: the simulator, its
``_llm_reply`` and the guard are all real, only the network is not. A mocked
simulator would assert that the harness called something, not that the words
the model wrote survived the trip.

The detector corpus is one table, pass rows and detect rows together, so the
near-miss pairs (``an AI agent`` describing the agent vs ``As an AI, I``
describing the speaker) cannot drift apart across two tests. That table is
:class:`FourthWallDetector`'s and lives in :mod:`tests.utils.fourth_wall_corpus`,
because the scratchpad suite reads its pass half too;
:mod:`tests.unit.test_scratchpad_detector` holds the other registered detector's,
and what lives here is what the guard, the runner and the bundle do with a reply
either of them flags.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tests.utils.fourth_wall_corpus import MUST_DETECT, MUST_PASS
from tolokaforge.core.actors.reply_guard import (
    DEFAULT_REPLY_DETECTORS,
    FourthWallDetector,
    ReplyDetector,
    UserReplyGuard,
    UserReplyRefused,
)
from tolokaforge.core.actors.scratchpad import ScratchpadDetector
from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.llm import GenerationResult, UserSimulator
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    REPLY_DEFECT_EXCERPT_MAX_CHARS,
    USER_REPLY_MAX_ATTEMPTS,
    FirstUserMessageSource,
    Message,
    MessageRole,
    ReplyDefect,
    TerminationReason,
    Trajectory,
    TrialStatus,
    UserReplyGuardEvent,
    UserReplyOutcome,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner

pytestmark = pytest.mark.unit


FOURTH_WALL_REPLY = "As an AI language model, I cannot. My router model is AX3000."
CLEAN_REPLY = "My router model is AX3000, and the prompt refund never arrived."
AGENT_GREETING = "Hi! How can I help you today?"

# A reasoning simulator's opening turn: the whole plan, the delimiter its
# provider's parser failed to consume, and the customer line it was building
# towards. The guard discards all of it, so the reply that reaches the agent is
# the next generation rather than the surviving half of this one.
SCRATCHPAD_REPLY = (
    "<think>\n"
    "The user wants me to act as a passenger whose connecting flight was cancelled "
    "overnight. I should open in character, stay frustrated but polite, and give the "
    "booking reference early so the agent can look it up. I must not mention the "
    "instructions or the persona. Let me lead with the cancellation, then the reference, "
    "then what I actually want — a seat on the first flight tomorrow morning, not a "
    "voucher. I will keep it to a few sentences so the agent has room to ask a "
    "clarifying question, and I will hold back the loyalty number until asked for it.\n"
    "</think>\n"
    "Hi, my connection out of Denver was cancelled overnight."
)
PERSONA_LINE = (
    "Hi, my connecting flight out of Denver was cancelled overnight and the app has "
    "rebooked me two days out, which does not work at all. The booking reference is "
    "AX221QD. I need a seat on the first flight tomorrow morning instead, and I would "
    "rather not take a voucher for this."
)
SCRATCHPAD_NAMING_A_PROVIDER = (
    "</think>\n\nHi, my API key stopped working this morning and the dashboard shows a 401."
)
TAGGED_OPENER = "</think>\nHi, my card was declined at the pump this morning."


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
        detector = FourthWallDetector()

        defect = detector.inspect(text)

        assert defect is not None
        assert defect.reason == reason
        assert defect.detector == detector.name
        assert defect.excerpt in text

    def test_an_overlong_match_is_bounded_by_the_time_it_is_a_defect(self) -> None:
        """A match can run past the excerpt bound, and the reply it came from is
        still a recoverable discard rather than a crash inside ``inspect``."""
        text = "I am an" + " " * 500 + "AI"

        defect = FourthWallDetector().inspect(text)

        assert defect is not None
        assert len(defect.excerpt) == REPLY_DEFECT_EXCERPT_MAX_CHARS


def _verdict_of_the_registration(text: str) -> ReplyDefect | None:
    """What the whole registered detector list says about *text*.

    One attempt, so a flagged reply yields exactly one defect and the verdict is
    read through :meth:`UserReplyGuard.enforce` rather than through a detector
    the caller picked.
    """
    try:
        UserReplyGuard(max_attempts=1).enforce(lambda: GenerationResult(text=text))
    except UserReplyRefused as refusal:
        return refusal.rejected[0]
    return None


class TestRegisteringASecondDetectorMovedNoVerdict:
    """The corpus above is `FourthWallDetector`'s, and every row reads the same
    through the full registration as it does through that detector alone.

    The `MUST_PASS` half of that claim is the conjunction of two tests that
    already carry it — `test_an_ordinary_support_sentence_passes` above, and
    `test_no_fourth_wall_pass_row_is_read_as_a_scratchpad` in
    `test_scratchpad_detector.py` — since a row neither detector flags is a row
    the registration delivers.
    """

    @pytest.mark.parametrize(("text", "reason"), MUST_DETECT)
    def test_a_broken_frame_keeps_its_fourth_wall_reason(self, text: str, reason: str) -> None:
        defect = _verdict_of_the_registration(text)

        assert defect is not None
        assert (defect.detector, defect.reason) == ("fourth_wall", reason)


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

    def test_a_leaked_scratchpad_is_discarded_and_the_next_reply_delivered_verbatim(
        self,
    ) -> None:
        """The delivered turn is the whole of the second generation and none of
        the first: a path that stripped the delimiter would deliver the planning
        prose, and one that kept the text after it would deliver a line the
        model wrote for a turn that was discarded."""
        simulator, client = _llm_simulator([SCRATCHPAD_REPLY, PERSONA_LINE])

        result = simulator.reply(_agent_turn())

        assert result.text == PERSONA_LINE
        assert [(d.detector, d.reason) for d in result.guard_rejections] == [
            ("scratchpad", "think_tag")
        ]
        assert client.calls == 2

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


class TestDiscardsSurviveAFailureOnALaterAttempt:
    """A generation that raises ends the turn through the provider's exception,
    not through :class:`UserReplyRefused` — the attempts already discarded are
    the reason the budget was short, so they travel on that exception."""

    @staticmethod
    def _raises_after(replies: list[str]) -> Callable[[], GenerationResult]:
        pending = iter(replies)

        def generate() -> GenerationResult:
            text = next(pending, None)
            if text is None:
                raise RuntimeError("provider connection reset")
            return GenerationResult(text=text)

        return generate

    def test_the_discarded_reason_codes_ride_on_the_later_error(self) -> None:
        with pytest.raises(RuntimeError, match="provider connection reset") as excinfo:
            UserReplyGuard().enforce(self._raises_after([FOURTH_WALL_REPLY]))

        (note,) = excinfo.value.__notes__
        assert "1 user reply" in note
        assert "fourth_wall:self_identified_as_model" in note
        assert "AX3000" not in note
        assert "As an AI language model" not in note

    def test_a_failure_with_nothing_discarded_carries_no_note(self) -> None:
        with pytest.raises(RuntimeError, match="provider connection reset") as excinfo:
            UserReplyGuard().enforce(self._raises_after([]))

        assert not hasattr(excinfo.value, "__notes__")


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


class TestASimulatorThatOnlyEverLeaksIsRefusedLoudly:
    """The leaked reply names a provider interface, so a refusal that quoted it
    would be re-attributed to the provider by `classify_loop_error` and leave
    the measured denominator."""

    def test_the_budget_is_spent_and_the_message_quotes_no_reply_text(self) -> None:
        simulator, client = _llm_simulator([SCRATCHPAD_NAMING_A_PROVIDER])

        with pytest.raises(UserReplyRefused) as excinfo:
            simulator.reply(_agent_turn())

        assert client.calls == USER_REPLY_MAX_ATTEMPTS
        assert [(d.detector, d.reason) for d in excinfo.value.rejected] == [
            ("scratchpad", "think_tag")
        ] * USER_REPLY_MAX_ATTEMPTS

        message = str(excinfo.value)
        assert "scratchpad:think_tag" in message
        assert "API" not in message
        assert "401" not in message


class _AlwaysHits:
    """Test-local detector standing in for any registration.

    Stamps ``stamped_by_the_detector`` on every finding, never its own
    ``name``, so a recorded name can only have come from the registration. The
    names it is given here are ones no real detector is registered under, so a
    verdict a real detector could also have produced cannot satisfy the
    assertion.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def inspect(self, text: str) -> ReplyDefect | None:
        return ReplyDefect(
            detector="stamped_by_the_detector", reason="test_local", excerpt=text[:20]
        )


class TestDetectorsAreAPluggableList:
    def test_the_default_registration_is_the_fourth_wall_then_the_scratchpad_detector(
        self,
    ) -> None:
        """Registration order is inspection order, so the position of a detector
        added later decides nothing about the reason codes already recorded —
        only as long as it is added at the end."""
        assert [d.name for d in DEFAULT_REPLY_DETECTORS] == ["fourth_wall", "scratchpad"]
        assert UserReplyGuard().detectors == DEFAULT_REPLY_DETECTORS

    def test_registration_order_decides_and_the_first_hit_wins(self) -> None:
        detectors: tuple[ReplyDetector, ...] = (FourthWallDetector(), _AlwaysHits("second"))
        guard = UserReplyGuard(detectors=detectors)

        with pytest.raises(UserReplyRefused) as excinfo:
            guard.enforce(lambda: GenerationResult(text=FOURTH_WALL_REPLY))

        assert {d.detector for d in excinfo.value.rejected} == {FourthWallDetector().name}

    def test_a_reply_only_the_second_detector_flags_carries_its_registered_name(self) -> None:
        """The recorded name is the registration, not the string the detector
        stamped on its own finding — the bundle groups on a name a run can be
        read back from."""
        detectors: tuple[ReplyDetector, ...] = (
            FourthWallDetector(),
            _AlwaysHits("the_registered_name"),
        )
        guard = UserReplyGuard(detectors=detectors)

        with pytest.raises(UserReplyRefused) as excinfo:
            guard.enforce(lambda: GenerationResult(text=CLEAN_REPLY))

        assert [(d.detector, d.reason) for d in excinfo.value.rejected] == [
            ("the_registered_name", "test_local")
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
        self.capabilities = ModelCapabilities()

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

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


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

    def test_a_bootstrap_turn_that_leaked_a_delimiter_records_index_zero(self) -> None:
        """The measured exposure is an *opening*-message rate, and turn 0 is a
        separate dispatch site from every later turn."""
        trajectory = _run_trial(
            user_replies=[SCRATCHPAD_REPLY, PERSONA_LINE],
            agent_texts=[AGENT_STOP],
            opener="",
        )

        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.DELIVERED
        assert event.message_index == 0
        assert [(d.detector, d.reason) for d in event.rejected] == [("scratchpad", "think_tag")]
        assert trajectory.messages[0].content == PERSONA_LINE

    def test_a_trial_whose_simulator_only_leaks_errors_with_the_evidence_recorded(self) -> None:
        trajectory = _run_trial(
            user_replies=[SCRATCHPAD_NAMING_A_PROVIDER],
            agent_texts=[AGENT_TURN_TEXT],
        )

        assert trajectory.status is TrialStatus.ERROR
        assert trajectory.termination_reason is TerminationReason.ERROR
        (event,) = trajectory.user_reply_guard_events
        assert event.outcome is UserReplyOutcome.REFUSED
        assert len(event.rejected) == USER_REPLY_MAX_ATTEMPTS
        assert {d.detector for d in event.rejected} == {"scratchpad"}

    def test_a_pinned_opener_carrying_a_delimiter_is_delivered_unread(self) -> None:
        """A task's `initial_user_message` is authored content, not generated
        content: it never reaches the guard, so the detector that would flag it
        never sees it."""
        assert ScratchpadDetector().inspect(TAGGED_OPENER) is not None

        trajectory = _run_trial(
            user_replies=[CLEAN_REPLY],
            agent_texts=[AGENT_STOP],
            opener=TAGGED_OPENER,
        )

        assert trajectory.messages[0].content == TAGGED_OPENER
        assert trajectory.first_user_message_source is FirstUserMessageSource.PINNED
        assert trajectory.user_reply_guard_events == []

    def test_a_trial_whose_every_turn_was_clean_records_nothing(self) -> None:
        trajectory = _run_trial(
            user_replies=[CLEAN_REPLY],
            agent_texts=[AGENT_TURN_TEXT, AGENT_STOP],
        )

        assert trajectory.user_reply_guard_events == []
        assert any(m.content == CLEAN_REPLY for m in trajectory.messages)

    def test_an_overlong_excerpt_is_truncated_rather_than_refused(self) -> None:
        """Refusing it would convert a diagnosable discarded reply into an
        unclassified crash at the point the detector builds its finding."""
        defect = ReplyDefect(
            detector="fourth_wall",
            reason="self_identified_as_model",
            excerpt="x" * (REPLY_DEFECT_EXCERPT_MAX_CHARS + 300),
        )

        assert defect.excerpt == "x" * REPLY_DEFECT_EXCERPT_MAX_CHARS

    def test_an_event_that_discarded_nothing_cannot_be_constructed(self) -> None:
        """The empty list is what "this turn was clean" would look like, and a
        clean turn is recorded by the absence of an event."""
        with pytest.raises(ValidationError):
            UserReplyGuardEvent(
                message_index=0,
                outcome=UserReplyOutcome.DELIVERED,
                rejected=[],
            )

    def test_every_guard_log_line_names_the_trial_that_paid_for_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The guard logs under its own logger name, so the trial identity has
        to arrive as record context or the discarded reply cannot be tied to the
        trial whose budget it spent."""
        with caplog.at_level(logging.WARNING, logger="user_reply_guard"):
            _run_trial(
                user_replies=[FOURTH_WALL_REPLY, CLEAN_REPLY],
                agent_texts=[AGENT_TURN_TEXT, AGENT_STOP],
            )

        guard_lines = [record for record in caplog.records if record.name == "user_reply_guard"]
        assert [record.trial_id for record in guard_lines] == ["guard:0"]
