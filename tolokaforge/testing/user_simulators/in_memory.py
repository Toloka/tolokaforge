"""The in-memory reference :class:`~tolokaforge.core.actors.user_simulator.UserSimulator`.

:class:`InMemoryUserSimulator` is the shortest simulator that satisfies the
:class:`~tolokaforge.core.actors.user_simulator.UserSimulator` Protocol. It runs
no provider and holds no state beyond one dialogue, so an implementer can read
the whole contract in one screen and copy it: a ``reply`` that returns a
:class:`~tolokaforge.core.llm.client.GenerationResult`, and a
``last_system_prompt`` the runner captures into the bundle's ``prompts.yaml``.

It is also the suite's own control. Every switch in :class:`SimulatorDefects`
breaks exactly one contract obligation without crashing, which is what lets
``tests/canonical/test_user_simulator_contract.py`` show each conformance
assertion failing on the simulator that violates the assertion it is written
for. A conformance kit nothing can fail is a kit that proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tolokaforge.core.actors.user_simulator import UserSimulatorContext
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models import Message
from tolokaforge.core.run_display_events import LLMCallObservation

__all__ = [
    "InMemoryUserSimulator",
    "InMemoryUserSimulatorCallLog",
    "SimulatorDefects",
    "in_memory_user_simulator_factory",
]

_DEFAULT_REPLY = "Yes, please go ahead."


@dataclass(frozen=True)
class SimulatorDefects:
    """One switch per obligation, each defaulting to "honoured".

    Each field turns off a single contract obligation without breaking the
    simulator, which is the point: each produces a plausible reply and a wrong
    downstream artifact rather than a crash, and each is the vector one
    conformance assertion exists to catch.
    """

    wrong_reply_type: bool = False
    """Return the raw reply string instead of a :class:`GenerationResult`.

    The runner reads ``.text`` / ``.tool_calls`` off the reply; a bare string
    is a plausible-looking value that has neither.
    """

    last_system_prompt_wrong_type: bool = False
    """Expose ``last_system_prompt`` as a non-string, non-``None`` value.

    The runner writes it verbatim to ``prompts.yaml``; anything but ``str | None``
    corrupts the bundle the graders and re-judge path read.
    """

    crash_on_simulator_config: bool = False
    """Refuse to build when ``simulator_config`` is non-empty.

    The engine passes ``actors.user.simulator_config`` through untouched; a
    simulator that cannot accept the passthrough cannot carry its own config.
    """


@dataclass
class InMemoryUserSimulatorCallLog:
    """What the simulator did, for orchestrator-level assertions."""

    replies: int = 0
    contexts: list[list[Message]] = field(default_factory=list)


class InMemoryUserSimulator:
    """Deterministic user simulator over a scripted flow.

    Replies with the next line from ``context.scripted_flow`` (falling back to a
    fixed acknowledgement) and records what it was asked, holding no state
    between dialogues except :attr:`call_log`.
    """

    def __init__(
        self,
        context: UserSimulatorContext,
        defects: SimulatorDefects | None = None,
    ) -> None:
        self._defects = defects or SimulatorDefects()
        if self._defects.crash_on_simulator_config and context.simulator_config:
            raise ValueError(
                "InMemoryUserSimulator refuses a non-empty simulator_config (defect switch)"
            )
        self._lines = [
            rule.get("user") or rule.get("default") or _DEFAULT_REPLY
            for rule in (context.scripted_flow or [])
        ]
        self.persona = context.persona
        self.simulator_config = dict(context.simulator_config)
        self.call_log = InMemoryUserSimulatorCallLog()
        # A wrong-type value here is inert until the runner writes it out; the
        # defect leaves it in place so the conformance assertion sees it.
        self.last_system_prompt: str | None = (
            0 if self._defects.last_system_prompt_wrong_type else None  # type: ignore[assignment]
        )

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        self.call_log.replies += 1
        self.call_log.contexts.append(list(context))
        if not self._defects.last_system_prompt_wrong_type:
            self.last_system_prompt = f"You are a simulated user ({self.persona})."
        text = self._next_line()
        if self._defects.wrong_reply_type:
            # Defect: emit a non-GenerationResult to prove the suite catches it.
            return text  # type: ignore[return-value]
        return GenerationResult(text=text, tool_calls=[])

    def _next_line(self) -> str:
        index = self.call_log.replies - 1
        if index < len(self._lines):
            return self._lines[index]
        return _DEFAULT_REPLY


def in_memory_user_simulator_factory(context: UserSimulatorContext) -> InMemoryUserSimulator:
    """A :data:`~tolokaforge.core.actors.user_simulator.UserSimulatorFactory` over the reference."""
    return InMemoryUserSimulator(context=context)
