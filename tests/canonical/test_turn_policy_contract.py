"""Pin the :class:`TurnPolicy` Protocol contract + built-in policy semantics.

Locks the surfaces later stages depend on:

* the Protocol is ``@runtime_checkable`` and both built-in policies pass
  ``isinstance``;
* :meth:`ConversationalTurnPolicy.bootstrap` short-circuits to a caller-provided
  ``initial_user_message`` and routes to the simulator otherwise —
  reproducing today's ``_seed_first_user_message`` branch at
  ``runner.py:508`` before the runner starts dispatching through the seam;
* :meth:`ConversationalTurnPolicy.next_actor` dispatches the user actor only
  when the previous agent turn produced no tool calls — reproducing today's
  ``_advance_user_turn`` gate at ``loop.py:337-341``;
* :meth:`AgentOnlyTurnPolicy.bootstrap` accepts a caller-provided seed and
  fails loud otherwise — the agent-monologue shape has no user simulator to
  synthesise turn 0 from, so a missing seed is a config error we surface at
  run-start rather than degrade into an empty user turn;
* :meth:`AgentOnlyTurnPolicy.next_actor` returns ``None`` unconditionally —
  the loop never dispatches a user actor in this mode, independent of loop
  state.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.actors.actor import (
    GenerationResult,
    LLMCallObservation,
    Message,
)
from tolokaforge.core.actors.turn_policy import (
    ActorTurn,
    AgentOnlyTurnPolicy,
    BootstrapDecision,
    ConversationalTurnPolicy,
    TurnPolicy,
    TurnState,
)
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import MessageRole
from tolokaforge.core.models.task_config import TaskConfig
from tolokaforge.core.plugin_registry import TurnPolicyContext

pytestmark = pytest.mark.canonical


class _InMemoryActor:
    """Scripted stand-in for a real :class:`Actor` implementation.

    Mirrors the fixture used in ``test_actor_contract.py``: no LLM client,
    empty ``tool_calls``, zero usage. Enough surface for a policy test to
    check identity of the actor the policy hands back.
    """

    def __init__(self, reply_text: str = "in-memory reply") -> None:
        self._reply_text = reply_text

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            text=self._reply_text,
            tool_calls=[],
            usage=Usage(),
            latency_s=0.0,
        )


def _task() -> TaskConfig:
    return TaskConfig(task_id="policy-fixture", description="canonical turn-policy test")


def _agent_message(text: str = "How can I help?") -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; ``ConversationalTurnPolicy``
    satisfies it via ``isinstance`` (not just structural type-hint
    compatibility)."""

    def test_conversational_policy_passes_isinstance(self) -> None:
        policy = ConversationalTurnPolicy(user_simulator=_InMemoryActor())
        assert isinstance(policy, TurnPolicy)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAPolicy:
            pass

        assert not isinstance(_NotAPolicy(), TurnPolicy)


class TestBootstrap:
    """``bootstrap`` short-circuits to a caller-provided initial message
    when it carries non-whitespace text, and otherwise flags the runner to
    dispatch the user simulator."""

    def test_returns_provided_message_and_skips_simulator(self) -> None:
        policy = ConversationalTurnPolicy(user_simulator=_InMemoryActor())

        decision = policy.bootstrap(_task(), "Please migrate the schema.")

        assert decision == BootstrapDecision(
            first_user_message="Please migrate the schema.",
            bootstrap_via_simulator=False,
        )

    def test_none_message_routes_to_simulator(self) -> None:
        policy = ConversationalTurnPolicy(user_simulator=_InMemoryActor())

        decision = policy.bootstrap(_task(), None)

        assert decision == BootstrapDecision(
            first_user_message=None,
            bootstrap_via_simulator=True,
        )

    def test_empty_or_whitespace_message_routes_to_simulator(self) -> None:
        policy = ConversationalTurnPolicy(user_simulator=_InMemoryActor())

        empty = policy.bootstrap(_task(), "")
        whitespace = policy.bootstrap(_task(), "   \n\t ")

        expected = BootstrapDecision(first_user_message=None, bootstrap_via_simulator=True)
        assert empty == expected
        assert whitespace == expected


class TestNextActor:
    """``next_actor`` dispatches the user actor only when the previous
    agent turn produced no tool calls; tool-call turns keep control on
    the agent so the tool results feed the next agent generation."""

    def test_no_tool_calls_dispatches_user(self) -> None:
        simulator = _InMemoryActor()
        policy = ConversationalTurnPolicy(user_simulator=simulator)
        state = TurnState(
            messages=[_agent_message()],
            last_agent_had_tool_calls=False,
            turn_index=1,
        )

        turn = policy.next_actor(state)

        assert turn == ActorTurn(actor_name="user", actor=simulator)
        assert isinstance(turn, ActorTurn)
        assert turn.actor is simulator

    def test_tool_call_turn_returns_none(self) -> None:
        policy = ConversationalTurnPolicy(user_simulator=_InMemoryActor())
        state = TurnState(
            messages=[_agent_message()],
            last_agent_had_tool_calls=True,
            turn_index=1,
        )

        assert policy.next_actor(state) is None


class TestAgentOnlyProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; ``AgentOnlyTurnPolicy``
    satisfies it via ``isinstance``, and the factory accepts a context
    with no user simulator — the whole point of the agent-monologue shape."""

    def test_agent_only_policy_passes_isinstance(self) -> None:
        assert isinstance(AgentOnlyTurnPolicy(), TurnPolicy)

    def test_factory_ignores_absent_user_simulator(self) -> None:
        from tolokaforge.core.actors.turn_policy import _agent_only_policy_factory

        policy = _agent_only_policy_factory(TurnPolicyContext(user_simulator=None))

        assert isinstance(policy, AgentOnlyTurnPolicy)


class TestAgentOnlyBootstrap:
    """``bootstrap`` accepts a caller-provided seed and fails loud otherwise.

    Agent-only mode has no user simulator to synthesise turn 0 from, so an
    empty / missing / whitespace-only ``initial_user_message`` is a config
    error surfaced at run-start rather than a silent empty-user-turn
    degrade.
    """

    def test_returns_provided_message_and_skips_simulator(self) -> None:
        decision = AgentOnlyTurnPolicy().bootstrap(_task(), "Migrate this crate to Rust.")

        assert decision == BootstrapDecision(
            first_user_message="Migrate this crate to Rust.",
            bootstrap_via_simulator=False,
        )

    def test_none_message_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="agent_only") as excinfo:
            AgentOnlyTurnPolicy().bootstrap(_task(), None)
        assert "policy-fixture" in str(excinfo.value)

    def test_empty_message_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="agent_only"):
            AgentOnlyTurnPolicy().bootstrap(_task(), "")

    def test_whitespace_only_message_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="agent_only"):
            AgentOnlyTurnPolicy().bootstrap(_task(), "   \n\t ")


class TestAgentOnlyNextActor:
    """``next_actor`` returns ``None`` unconditionally — the mode never
    dispatches a user turn regardless of what the previous agent turn
    did.
    """

    def test_no_tool_calls_returns_none(self) -> None:
        state = TurnState(
            messages=[_agent_message()],
            last_agent_had_tool_calls=False,
            turn_index=1,
        )

        assert AgentOnlyTurnPolicy().next_actor(state) is None

    def test_tool_call_turn_returns_none(self) -> None:
        state = TurnState(
            messages=[_agent_message()],
            last_agent_had_tool_calls=True,
            turn_index=1,
        )

        assert AgentOnlyTurnPolicy().next_actor(state) is None
