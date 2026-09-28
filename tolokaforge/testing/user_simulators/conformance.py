"""The conformance suite every user simulator runs against itself.

:class:`~tolokaforge.core.actors.user_simulator.UserSimulator` is a small
contract — a ``reply`` that returns a
:class:`~tolokaforge.core.llm.client.GenerationResult`, a ``last_system_prompt``
the runner reads, and a factory that accepts the engine's
:class:`~tolokaforge.core.actors.user_simulator.UserSimulatorContext` — but each
part is read downstream (the loop reads the reply, the bundle records the prompt,
the conductor passes the config through), so a break shows up as a wrong artifact
rather than an exception where the simulator lives.

Each test drives the factory under test and reads what a conforming simulator
must produce, never the implementation. Adoption is the repo's standard suite
shape — subclass and supply one fixture::

    from tolokaforge.testing.user_simulators import UserSimulatorConformanceSuite

    class TestMySimulatorConformance(UserSimulatorConformanceSuite):
        @pytest.fixture
        def simulator_factory(self):
            return my_user_simulator_factory

The base class carries no ``Test`` prefix so pytest does not collect it.

The suite drives ``scripted`` mode only, so it never reaches a provider: an
LLM-backed simulator's on-the-wire behaviour is the implementer's own to test.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.actors.user_simulator import (
    UserSimulator,
    UserSimulatorContext,
    UserSimulatorFactory,
)
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models import Message, MessageRole

__all__ = [
    "UserSimulatorConformanceSuite",
    "run_reply",
    "scripted_context",
]

_AGENT_TURN = "Hi! How can I help you today?"


def scripted_context(*, simulator_config: dict | None = None) -> UserSimulatorContext:
    """A no-network context the suite builds every simulator from."""
    return UserSimulatorContext(
        mode="scripted",
        persona="cooperative",
        backstory=None,
        scripted_flow=[{"default": "Yes, please proceed."}],
        tool_schemas=None,
        simulator_config=dict(simulator_config or {}),
    )


def run_reply(simulator: UserSimulator) -> object:
    """Ask the simulator for one reply to a single agent turn."""
    return simulator.reply([Message(role=MessageRole.ASSISTANT, content=_AGENT_TURN)])


class UserSimulatorConformanceSuite:
    """Subclass and override ``simulator_factory`` to certify one simulator."""

    @pytest.fixture
    def simulator_factory(self) -> UserSimulatorFactory:
        raise NotImplementedError(
            "subclasses of UserSimulatorConformanceSuite must override the "
            "`simulator_factory` fixture to return a UserSimulatorFactory — the "
            "same callable the `tolokaforge.user_simulators` entry point resolves to"
        )

    def test_factory_builds_a_user_simulator(self, simulator_factory: UserSimulatorFactory) -> None:
        """The factory's product satisfies the runtime-checkable Protocol."""
        simulator = simulator_factory(scripted_context())
        assert isinstance(simulator, UserSimulator), (
            f"{type(simulator).__name__} does not satisfy the UserSimulator Protocol; "
            "the conductor resolves the factory and dispatches `reply` on whatever it "
            "returns, and the runner reads `last_system_prompt` off it"
        )

    def test_reply_returns_a_generation_result(
        self, simulator_factory: UserSimulatorFactory
    ) -> None:
        """The turn loop reads ``.text`` and ``.tool_calls`` off the reply.

        A reply that is not a :class:`GenerationResult` has neither, so the user's
        turn reaches the agent as nothing at all.
        """
        simulator = simulator_factory(scripted_context())
        result = run_reply(simulator)
        assert isinstance(result, GenerationResult), (
            f"reply returned {type(result).__name__}; the turn loop reads `.text` and "
            "`.tool_calls` off a GenerationResult and has no other shape to fall back on"
        )

    def test_last_system_prompt_is_str_or_none(
        self, simulator_factory: UserSimulatorFactory
    ) -> None:
        """The runner writes ``last_system_prompt`` verbatim into ``prompts.yaml``.

        It is ``None`` before any LLM dispatch and a string after one; anything
        else corrupts the bundle the graders and the re-judge path read.
        """
        simulator = simulator_factory(scripted_context())
        run_reply(simulator)
        value = simulator.last_system_prompt
        assert value is None or isinstance(value, str), (
            f"last_system_prompt is {type(value).__name__}; the runner writes it "
            "straight into prompts.yaml, so it must be str | None"
        )

    def test_factory_accepts_a_simulator_config_passthrough(
        self, simulator_factory: UserSimulatorFactory
    ) -> None:
        """``actors.user.simulator_config`` reaches the factory untouched.

        The engine never interprets its keys, so a simulator that cannot be built
        with one present cannot carry its own configuration.
        """
        try:
            simulator = simulator_factory(scripted_context(simulator_config={"opaque": "value"}))
        except Exception as exc:  # noqa: BLE001 — surfaced as a conformance failure
            raise AssertionError(
                "the factory rejected a non-empty simulator_config; the engine passes "
                f"actors.user.simulator_config through verbatim: {exc}"
            ) from exc
        assert isinstance(simulator, UserSimulator)
