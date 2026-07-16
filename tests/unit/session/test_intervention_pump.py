"""Unit tests for :class:`SessionInterventionHandler` — M1 sub-5a.

Covers the intervention pump's InjectMessage handling, rejection of
not-yet-supported kinds, ack-outcome updates on the durable session trace,
and end-to-end integration with :class:`ToolCallingLoop`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.loop import LoopConfig, ToolCallingLoop
from tolokaforge.core.models import Message, MessageRole
from tolokaforge.session import (
    ApproveTool,
    EditState,
    InjectMessage,
    InProcessTrialSession,
    Kill,
    ParticipantRole,
    Pause,
    RejectTool,
    Resume,
    SessionInterventionHandler,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _submit_inject(session: InProcessTrialSession, content: str = "try /v2/auth") -> InjectMessage:
    handle = session.attach(f"p_{content}", ParticipantRole.PARTICIPANT)
    intervention = InjectMessage(
        trial_id=session.trial_id,
        attach_to_seq=0,
        participant_id=handle.participant_id,
        timestamp=_NOW,
        content=content,
    )
    session.interventions().submit(handle, intervention)
    return intervention


class TestInjectMessageApplication:
    def test_pump_appends_user_message_from_inject(self):
        session = InProcessTrialSession(trial_id="t:0")
        _submit_inject(session, "please try again")
        handler = SessionInterventionHandler(session)

        messages: list[Message] = []
        handler.drain_and_apply(messages)

        assert len(messages) == 1
        assert messages[0].role == MessageRole.USER
        assert messages[0].content == "please try again"

    def test_pump_updates_ack_outcome_to_accepted(self):
        session = InProcessTrialSession(trial_id="t:0")
        intervention = _submit_inject(session)
        handler = SessionInterventionHandler(session)

        # Before drain: ack is "queued" (returned by submit)
        snap_before = session.snapshot()
        assert snap_before["interventions"][0]["ack_outcome"] == "queued"

        handler.drain_and_apply([])

        # After drain: ack is "accepted"
        snap_after = session.snapshot()
        assert snap_after["interventions"][0]["ack_outcome"] == "accepted"
        assert snap_after["interventions"][0]["ack_reason"] is None
        # Sanity: same intervention record, not a duplicate
        assert len(snap_after["interventions"]) == 1
        assert intervention.content == "try /v2/auth"  # object still intact

    def test_pump_applies_multiple_injects_in_order(self):
        session = InProcessTrialSession(trial_id="t:0")
        for text in ["first", "second", "third"]:
            _submit_inject(session, text)
        handler = SessionInterventionHandler(session)

        messages: list[Message] = []
        handler.drain_and_apply(messages)

        assert [m.content for m in messages] == ["first", "second", "third"]

    def test_second_drain_is_noop_after_processing(self):
        session = InProcessTrialSession(trial_id="t:0")
        _submit_inject(session)
        handler = SessionInterventionHandler(session)

        handler.drain_and_apply([])
        # Now the queue is empty; second call must not re-apply
        messages: list[Message] = []
        handler.drain_and_apply(messages)
        assert messages == []


class TestNotYetSupportedKindsRejected:
    def _session_with(self, intervention_factory) -> tuple[InProcessTrialSession, object]:
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        intervention = intervention_factory(handle.participant_id)
        session.interventions().submit(handle, intervention)
        return session, intervention

    @pytest.mark.parametrize(
        "factory,expected_reason_substring",
        [
            (
                lambda pid: Kill(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id=pid,
                    timestamp=_NOW,
                    reason="stop please",
                ),
                "Kill",
            ),
            (
                lambda pid: Pause(
                    trial_id="t:0", attach_to_seq=0, participant_id=pid, timestamp=_NOW
                ),
                "Pause",
            ),
            (
                lambda pid: Resume(
                    trial_id="t:0", attach_to_seq=0, participant_id=pid, timestamp=_NOW
                ),
                "Resume",
            ),
            (
                lambda pid: ApproveTool(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id=pid,
                    timestamp=_NOW,
                    call_id="c1",
                ),
                "ApproveTool",
            ),
            (
                lambda pid: RejectTool(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id=pid,
                    timestamp=_NOW,
                    call_id="c1",
                ),
                "RejectTool",
            ),
            (
                lambda pid: EditState(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id=pid,
                    timestamp=_NOW,
                    state_key="k",
                    new_value=1,
                ),
                "EditState",
            ),
        ],
    )
    def test_unsupported_kind_rejected_with_reason(self, factory, expected_reason_substring):
        session, intervention = self._session_with(factory)
        handler = SessionInterventionHandler(session)
        messages: list[Message] = []

        handler.drain_and_apply(messages)

        # No messages appended for unsupported kinds
        assert messages == []
        # Trace records the rejection with a documented reason
        snap = session.snapshot()
        assert snap["interventions"][0]["ack_outcome"] == "rejected"
        assert expected_reason_substring in snap["interventions"][0]["ack_reason"]


class TestLoopIntegration:
    """Drive :class:`ToolCallingLoop` with the pump attached; verify that an
    InjectMessage submitted between turns actually reaches the next agent
    generate() call.
    """

    def test_injected_message_appears_in_agent_context_next_turn(self):
        session = InProcessTrialSession(trial_id="t:0")
        # Simulate an external participant queuing an intervention before turn 1
        _submit_inject(session, "try /v2/auth instead")

        handler = SessionInterventionHandler(session)

        # First generation triggers no more interventions; second-turn
        # terminates the loop. We assert the second generate call sees the
        # injected user message in its messages arg.
        seen_messages_per_call: list[list[Message]] = []

        def fake_generate(system, messages, tools, tool_choice="auto"):
            seen_messages_per_call.append(list(messages))
            return GenerationResult(text="ok", tool_calls=[], usage=Usage())

        llm_client = MagicMock()
        llm_client.generate.side_effect = fake_generate

        loop = ToolCallingLoop(
            llm_client=llm_client,
            tool_executor=MagicMock(),
            tool_schemas=[],
            config=LoopConfig(max_turns=1, episode_timeout_s=60),
            metrics=MagicMock(),
            should_terminate=lambda result, turn, messages: None,
            logger=init_trial_logger("t:0", verbose=False, strict=False),
            intervention_handler=handler,
        )
        loop.run(system_prompt="sys", messages=[], start_time=time.time())

        # The pump ran before turn 0's generate; the message list at generate
        # time contains the injected user message.
        assert len(seen_messages_per_call) == 1
        contents = [m.content for m in seen_messages_per_call[0] if m.role == MessageRole.USER]
        assert "try /v2/auth instead" in contents


class TestNullPumpIsNoOp:
    """The default :data:`_NULL_INTERVENTION_HANDLER` must be a byte-identical
    no-op — sealed batch mode incurs zero behavior change from sub-5a.
    """

    def test_default_intervention_handler_is_no_op(self):
        from tolokaforge.core.loop import _NULL_INTERVENTION_HANDLER

        messages: list[Message] = []
        _NULL_INTERVENTION_HANDLER.drain_and_apply(messages)
        assert messages == []


class TestOutcomeUpdateSemantics:
    def test_record_intervention_outcome_matches_by_object_identity(self):
        session = InProcessTrialSession(trial_id="t:0")
        inj = _submit_inject(session, "first")
        session.record_intervention_outcome(inj, "accepted", "test-reason")

        snap = session.snapshot()
        assert snap["interventions"][0]["ack_outcome"] == "accepted"
        assert snap["interventions"][0]["ack_reason"] == "test-reason"

    def test_record_intervention_outcome_is_noop_for_unknown_intervention(self):
        session = InProcessTrialSession(trial_id="t:0")
        _submit_inject(session, "in-history")

        # An intervention object that was never submitted through this session
        unknown = InjectMessage(
            trial_id="t:0",
            attach_to_seq=0,
            participant_id="ghost",
            timestamp=_NOW,
            content="not-in-history",
        )
        # Should not raise; simply no-op
        session.record_intervention_outcome(unknown, "accepted", None)

        snap = session.snapshot()
        assert len(snap["interventions"]) == 1
        assert snap["interventions"][0]["ack_outcome"] == "queued"
