"""Pin the :class:`Actor` Protocol contract — runtime check + round-trip.

Two implementations are checked: :class:`UserSimulator` (the historical
concrete actor, constructed in scripted mode with a minimal flow) and a
purpose-built ``_InMemoryActor`` fixture in this file that locks the
contract's expected value-object shape for future actor kinds (adversary,
oracle, evaluator).

Layer-2 of the multi-actor design (see plan
``hazy-doodling-lark.md`` § Layer 2): types-and-tests-only. No runtime
behaviour changes here.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.actors.actor import (
    Actor,
    GenerationResult,
    LLMCallObservation,
    Message,
)
from tolokaforge.core.llm.client import UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import MessageRole

pytestmark = pytest.mark.canonical


class _InMemoryActor:
    """Minimal :class:`Actor` for locking the Protocol shape.

    Records every ``reply`` call; returns a scripted :class:`GenerationResult`
    with empty ``tool_calls`` and zero-usage. No wire I/O, no LLM client.
    """

    def __init__(self, reply_text: str = "in-memory reply") -> None:
        self._reply_text = reply_text
        self.calls: list[tuple[tuple[Message, ...], LLMCallObservation | None]] = []

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        self.calls.append((tuple(context), observation))
        return GenerationResult(
            text=self._reply_text,
            tool_calls=[],
            usage=Usage(),
            latency_s=0.0,
        )


def _scripted_user_simulator() -> UserSimulator:
    """Build a scripted :class:`UserSimulator` — no LLM client wired."""
    return UserSimulator(
        mode="scripted",
        scripted_flow=[{"user": "Please complete step one."}],
    )


def _scripted_context() -> list[Message]:
    """One agent turn to react to — enough to exercise ``reply``."""
    return [
        Message(role=MessageRole.ASSISTANT, content="What would you like me to do?"),
    ]


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy
    it via ``isinstance`` (not just structural type-hint compatibility).
    """

    def test_user_simulator_passes_isinstance(self) -> None:
        assert isinstance(_scripted_user_simulator(), Actor)

    def test_in_memory_actor_passes_isinstance(self) -> None:
        assert isinstance(_InMemoryActor(), Actor)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAnActor:
            pass

        assert not isinstance(_NotAnActor(), Actor)


class TestReplyRoundTrip:
    """Given a scripted context, every :class:`Actor` implementation
    returns a :class:`GenerationResult` whose fields match the shape
    downstream loop code assumes: ``text`` is a ``str`` (possibly empty),
    ``tool_calls`` is a ``list``, ``usage`` is a :class:`Usage` instance,
    ``latency_s`` is a ``float``.
    """

    def test_user_simulator_reply_shape(self) -> None:
        result = _scripted_user_simulator().reply(_scripted_context())
        assert isinstance(result, GenerationResult)
        assert isinstance(result.text, str)
        assert result.text
        assert isinstance(result.tool_calls, list)
        assert isinstance(result.usage, Usage)
        assert isinstance(result.latency_s, float)

    def test_in_memory_actor_reply_shape(self) -> None:
        actor = _InMemoryActor(reply_text="ack")
        result = actor.reply(_scripted_context())
        assert isinstance(result, GenerationResult)
        assert result.text == "ack"
        assert result.tool_calls == []
        assert isinstance(result.usage, Usage)
        assert result.usage.prompt_tokens == 0
        assert result.usage.completion_tokens == 0
        assert result.latency_s == 0.0

    def test_in_memory_actor_records_invocation(self) -> None:
        actor = _InMemoryActor()
        context = _scripted_context()
        actor.reply(context, observation=None)
        assert len(actor.calls) == 1
        recorded_context, recorded_observation = actor.calls[0]
        assert recorded_context == tuple(context)
        assert recorded_observation is None
