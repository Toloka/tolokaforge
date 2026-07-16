"""Unit tests for :class:`SessionInterventionHandler` — M1 sub-5a.

Covers the intervention pump's InjectMessage handling, rejection of
not-yet-supported kinds, ack-outcome updates on the durable session trace,
and end-to-end integration with :class:`ToolCallingLoop`.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.loop import LoopConfig, TerminationDecision, ToolCallingLoop
from tolokaforge.core.models import Message, MessageRole, TerminationReason, ToolCall, TrialStatus
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


def _tool_call_with_id(id_: str, name: str, args: dict):
    return ToolCall(id=id_, name=name, arguments=args)


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


class TestKillHandling:
    """Kill terminates the loop cleanly with USER_STOP and the participant's
    reason. Non-kill drained interventions at the same pause point are marked
    ``superseded``.
    """

    def _submit_kill(
        self,
        session: InProcessTrialSession,
        participant_id: str = "p_kill",
        role: ParticipantRole = ParticipantRole.PARTICIPANT,
        reason: str = "give up",
    ):
        handle = session.attach(participant_id, role)
        intervention = Kill(
            trial_id=session.trial_id,
            attach_to_seq=0,
            participant_id=handle.participant_id,
            timestamp=_NOW,
            reason=reason,
        )
        session.interventions().submit(handle, intervention)
        return handle, intervention

    def test_kill_returns_termination_decision_with_user_stop(self):
        session = InProcessTrialSession(trial_id="t:0")
        _, kill = self._submit_kill(session, reason="stop please")
        handler = SessionInterventionHandler(session)

        decision = handler.drain_and_apply([])

        assert decision is not None
        assert decision.reason == TerminationReason.USER_STOP
        assert decision.status == TrialStatus.FAILED
        assert "stop please" in decision.system_message
        assert kill.participant_id in decision.system_message

    def test_kill_recorded_as_accepted_in_trace(self):
        session = InProcessTrialSession(trial_id="t:0")
        self._submit_kill(session)
        handler = SessionInterventionHandler(session)

        handler.drain_and_apply([])

        snap = session.snapshot()
        assert snap["interventions"][0]["ack_outcome"] == "accepted"

    def test_kill_supersedes_concurrent_inject(self):
        """A Kill and an Inject drained at the same pause point: Kill wins,
        Inject is superseded, ``messages`` is unchanged.
        """
        session = InProcessTrialSession(trial_id="t:0")
        _submit_inject(session, "please try again")
        self._submit_kill(session, reason="never mind")
        handler = SessionInterventionHandler(session)

        messages: list[Message] = []
        decision = handler.drain_and_apply(messages)

        assert decision is not None
        assert messages == []  # Inject NOT applied

        outcomes = {
            rec["intervention"]["kind"]: rec["ack_outcome"]
            for rec in session.snapshot()["interventions"]
        }
        assert outcomes["inject_message"] == "superseded"
        assert outcomes["kill"] == "accepted"


class TestRolePriority:
    """Two Kill interventions from different roles at the same pause point:
    admin > participant. Within a tier, later submission wins.
    """

    def _submit_kill_from(
        self,
        session: InProcessTrialSession,
        participant_id: str,
        role: ParticipantRole,
        reason: str,
    ) -> Kill:
        handle = session.attach(participant_id, role)
        intervention = Kill(
            trial_id=session.trial_id,
            attach_to_seq=0,
            participant_id=participant_id,
            timestamp=_NOW,
            reason=reason,
        )
        session.interventions().submit(handle, intervention)
        return intervention

    def test_admin_kill_beats_participant_kill(self):
        session = InProcessTrialSession(trial_id="t:0")
        # Participant kill submitted FIRST, admin kill submitted SECOND —
        # admin still wins on role priority (not submit order).
        self._submit_kill_from(session, "p_user", ParticipantRole.PARTICIPANT, "user says stop")
        self._submit_kill_from(session, "p_admin", ParticipantRole.ADMIN, "admin says stop")

        handler = SessionInterventionHandler(session)
        decision = handler.drain_and_apply([])

        assert decision is not None
        assert "admin says stop" in decision.system_message
        assert "p_admin" in decision.system_message

    def test_later_submitted_wins_within_same_tier(self):
        """Two participant-role kills: later submission wins (stable sort +
        reversed index in the priority tuple).
        """
        session = InProcessTrialSession(trial_id="t:0")
        self._submit_kill_from(session, "p_first", ParticipantRole.PARTICIPANT, "first reason")
        self._submit_kill_from(session, "p_second", ParticipantRole.PARTICIPANT, "second reason")

        handler = SessionInterventionHandler(session)
        decision = handler.drain_and_apply([])

        assert decision is not None
        assert "second reason" in decision.system_message
        assert "p_second" in decision.system_message

    def test_losing_kill_marked_superseded_with_reference_to_winner(self):
        session = InProcessTrialSession(trial_id="t:0")
        self._submit_kill_from(session, "p_user", ParticipantRole.PARTICIPANT, "user reason")
        self._submit_kill_from(session, "p_admin", ParticipantRole.ADMIN, "admin reason")

        handler = SessionInterventionHandler(session)
        handler.drain_and_apply([])

        snap = session.snapshot()
        outcomes_by_pid = {rec["participant_id"]: rec for rec in snap["interventions"]}
        assert outcomes_by_pid["p_admin"]["ack_outcome"] == "accepted"
        assert outcomes_by_pid["p_user"]["ack_outcome"] == "superseded"
        assert "p_admin" in outcomes_by_pid["p_user"]["ack_reason"]


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


class TestLoopIntegrationKill:
    """Kill submitted between turns actually terminates the running loop."""

    def test_kill_terminates_loop_with_user_stop(self):
        session = InProcessTrialSession(trial_id="t:0")
        # Queue a Kill before turn 0's generate runs
        handle = session.attach("p_kill", ParticipantRole.PARTICIPANT)
        session.interventions().submit(
            handle,
            Kill(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p_kill",
                timestamp=_NOW,
                reason="operator abort",
            ),
        )
        handler = SessionInterventionHandler(session)

        # If Kill were ignored, this side_effect would run and the loop
        # would call generate 10 times (max_turns). If Kill works, the loop
        # terminates before the first generate and the mock is never called.
        llm_client = MagicMock()
        llm_client.generate.side_effect = lambda *a, **kw: GenerationResult(
            text="never runs", tool_calls=[], usage=Usage()
        )

        loop = ToolCallingLoop(
            llm_client=llm_client,
            tool_executor=MagicMock(),
            tool_schemas=[],
            config=LoopConfig(max_turns=10, episode_timeout_s=60),
            metrics=MagicMock(),
            should_terminate=lambda result, turn, messages: None,
            logger=init_trial_logger("t:0", verbose=False, strict=False),
            intervention_handler=handler,
        )
        messages: list[Message] = []
        outcome = loop.run(system_prompt="sys", messages=messages, start_time=time.time())

        assert outcome.termination_reason == TerminationReason.USER_STOP
        assert outcome.status == TrialStatus.FAILED
        llm_client.generate.assert_not_called()
        # A system message recording the kill lands on the transcript
        system_contents = [m.content for m in messages if m.role == MessageRole.SYSTEM]
        assert any("operator abort" in c for c in system_contents)


class TestPauseResumeFlow:
    """Pause enters a poll loop; Resume exits it. Both emit acknowledgement
    events on the session so attached participants can see the state
    transitions in real time.
    """

    def _submit(self, session, intervention, participant_id="p1", role=None):
        if role is None:
            role = ParticipantRole.PARTICIPANT
        # Reuse an already-attached handle when it exists (matched by pid)
        if participant_id in session.attached_participant_ids:
            # get_or_create shim via drain_pending_interventions history
            for slot_pid in session.attached_participant_ids:
                if slot_pid == participant_id:
                    from tolokaforge.session.protocols import ParticipantHandle

                    handle = ParticipantHandle(
                        participant_id=participant_id,
                        role=role,
                        trial_id=session.trial_id,
                    )
                    break
        else:
            handle = session.attach(participant_id, role)
        session.interventions().submit(handle, intervention)

    def test_pause_then_resume_returns_none_and_emits_ack_events(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        handle = session.attach("p_pauser", ParticipantRole.PARTICIPANT)
        session.interventions().submit(
            handle,
            Pause(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p_pauser",
                timestamp=_NOW,
            ),
        )

        # Fire Resume from a background thread after a short delay so the
        # pause loop exits.
        def resume_after_delay():
            time.sleep(0.3)
            resume_handle = session.attach("p_resumer", ParticipantRole.PARTICIPANT)
            session.interventions().submit(
                resume_handle,
                Resume(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id="p_resumer",
                    timestamp=_NOW,
                ),
            )

        threading.Thread(target=resume_after_delay, daemon=True).start()

        result = handler.drain_and_apply([])
        assert result is None  # Resume path returns None to continue the loop

        # Trace records both Pause and Resume as accepted; PauseAcknowledged
        # and ResumeAcknowledged events landed in event history.
        snap = session.snapshot()
        outcomes_by_kind = {
            rec["intervention"]["kind"]: rec["ack_outcome"] for rec in snap["interventions"]
        }
        assert outcomes_by_kind["pause"] == "accepted"
        assert outcomes_by_kind["resume"] == "accepted"

        event_kinds = [e["kind"] for e in snap["events"]]
        assert "pause_acknowledged" in event_kinds
        assert "resume_acknowledged" in event_kinds

    def test_kill_during_pause_supersedes_and_terminates(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        pause_handle = session.attach("p_pauser", ParticipantRole.PARTICIPANT)
        session.interventions().submit(
            pause_handle,
            Pause(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p_pauser",
                timestamp=_NOW,
            ),
        )

        def kill_after_delay():
            time.sleep(0.3)
            kill_handle = session.attach("p_killer", ParticipantRole.PARTICIPANT)
            session.interventions().submit(
                kill_handle,
                Kill(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id="p_killer",
                    timestamp=_NOW,
                    reason="cancel the trial",
                ),
            )

        threading.Thread(target=kill_after_delay, daemon=True).start()

        result = handler.drain_and_apply([])
        assert result is not None
        assert result.reason == TerminationReason.USER_STOP
        assert "cancel the trial" in result.system_message

        # ResumeAcknowledged still emitted (the pause did end, just not via Resume)
        event_kinds = [e["kind"] for e in session.snapshot()["events"]]
        assert "pause_acknowledged" in event_kinds
        assert "resume_acknowledged" in event_kinds

    def test_pause_returns_timeout_decision_when_check_timeout_fires(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        handle = session.attach("p_pauser", ParticipantRole.PARTICIPANT)
        session.interventions().submit(
            handle,
            Pause(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p_pauser",
                timestamp=_NOW,
            ),
        )

        timeout_decision = TerminationDecision(
            reason=TerminationReason.TIMEOUT,
            system_message="Episode timeout hit while paused.",
            status=TrialStatus.TIMEOUT,
        )
        # First call to check_timeout returns None; second returns the
        # timeout decision. The pump loops on _PAUSE_POLL_INTERVAL_S = 0.1 s,
        # so we expect ~0.2 s total.
        call_count = {"n": 0}

        def check_timeout():
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return timeout_decision
            return None

        result = handler.drain_and_apply([], check_timeout=check_timeout)
        assert result is timeout_decision


class TestToolApproval:
    """ApproveTool / RejectTool interventions consulted via intercept_tool_call
    at the tool-call seam.
    """

    def test_no_pending_decision_returns_none(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)
        # No ApproveTool / RejectTool ever submitted
        assert handler.intercept_tool_call("c1", "lookup", {}) is None

    def test_reject_tool_returned_and_recorded_accepted(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        reject = RejectTool(
            trial_id="t:0",
            attach_to_seq=0,
            participant_id="p1",
            timestamp=_NOW,
            call_id="c1",
            reason="unsafe payload",
        )
        session.interventions().submit(handle, reject)

        # Drain at turn boundary to queue the decision
        handler.drain_and_apply([])
        # Now the tool-call seam sees the decision
        decision = handler.intercept_tool_call("c1", "lookup", {"q": "x"})

        assert decision is not None
        assert decision.action == "reject"
        assert decision.reason == "unsafe payload"

        # Ack outcome recorded on tool-call-seam consumption
        snap = session.snapshot()
        assert snap["interventions"][0]["ack_outcome"] == "accepted"

    def test_approve_tool_returned_and_recorded(self):
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        approve = ApproveTool(
            trial_id="t:0",
            attach_to_seq=0,
            participant_id="p1",
            timestamp=_NOW,
            call_id="c1",
            reason="ok",
        )
        session.interventions().submit(handle, approve)

        handler.drain_and_apply([])
        decision = handler.intercept_tool_call("c1", "lookup", {})

        assert decision is not None
        assert decision.action == "approve"

    def test_reject_tool_short_circuits_executor_in_loop(self):
        """End-to-end: RejectTool submitted before the tool fires; the loop
        sees the reject at the tool-call seam and synthesizes a tool-error
        message without invoking the executor.
        """
        session = InProcessTrialSession(trial_id="t:0")
        handler = SessionInterventionHandler(session)

        # Pre-submit a RejectTool matching call_id "c_A"
        handle = session.attach("p_gate", ParticipantRole.PARTICIPANT)
        session.interventions().submit(
            handle,
            RejectTool(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p_gate",
                timestamp=_NOW,
                call_id="c_A",
                reason="policy violation",
            ),
        )

        tool_call = _tool_call_with_id(id_="c_A", name="lookup", args={"q": "x"})
        llm_client = MagicMock()
        # First generate emits the tool call; second stops.
        llm_client.generate.side_effect = [
            GenerationResult(text="calling tool", tool_calls=[tool_call], usage=Usage()),
            GenerationResult(text="done after reject", tool_calls=[], usage=Usage()),
        ]

        tool_executor = MagicMock()
        # If the executor is invoked, the test fails: RejectTool should
        # short-circuit it.
        tool_executor.execute.side_effect = AssertionError("executor should not be called")

        loop = ToolCallingLoop(
            llm_client=llm_client,
            tool_executor=tool_executor,
            tool_schemas=[],
            config=LoopConfig(max_turns=2, episode_timeout_s=60),
            metrics=MagicMock(),
            should_terminate=lambda result, turn, messages: None,
            logger=init_trial_logger("t:0", verbose=False, strict=False),
            intervention_handler=handler,
        )
        messages: list[Message] = []
        loop.run(system_prompt="sys", messages=messages, start_time=time.time())

        tool_executor.execute.assert_not_called()
        tool_role_msgs = [m for m in messages if m.role == MessageRole.TOOL]
        assert any("policy violation" in m.content for m in tool_role_msgs)


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
