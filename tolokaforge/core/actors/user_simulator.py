"""User-simulator Protocol and entry-point registry seam.

Realizes ADR-0011's named "User-simulator Protocol lift", mirroring ADR-0050's
``AgentLoop`` seam. The engine's built-in simulator
(:class:`~tolokaforge.core.llm.client.BuiltinUserSimulator`) registers under the
``tolokaforge.user_simulators`` entry-point group as ``builtin`` and resolves
through :func:`~tolokaforge.core.plugin_registry.load_user_simulator` like any
third-party simulator. A downstream package — a benchmark adapter that must
reproduce another harness's dialogue, say — registers its own simulator
alongside it and selects it with ``actors.user.simulator`` without a framework
PR.

The Protocol, the context and the factory alias are declared together here; the
built-in factory lives beside the concrete class in
:mod:`tolokaforge.core.llm.client` (as ``AgentLoop``'s built-in factory lives
beside ``ToolCallingLoop``), and :mod:`tolokaforge.core.plugin_registry` owns
only the group constant, the loader and the listing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.core.actors.actor import Actor

if TYPE_CHECKING:
    from tolokaforge.core.models import ModelConfig, RateLimitProbeConfig
    from tolokaforge.core.models.task_config import UserToolTurns

__all__ = [
    "UserSimulator",
    "UserSimulatorContext",
    "UserSimulatorFactory",
]


@runtime_checkable
class UserSimulator(Actor, Protocol):
    """A user actor the conductor builds per trial and the turn loop dispatches.

    Extends the :class:`~tolokaforge.core.actors.actor.Actor` reply contract
    with the one attribute the runner reads off a simulator directly:
    ``last_system_prompt``. ``TrialRunner`` captures it after the first user turn
    and writes it to the trial bundle's ``prompts.yaml``; a simulator that never
    dispatches an LLM turn leaves it ``None``.

    A factory builds an implementation from a :class:`UserSimulatorContext`;
    implementations resolve through the ``tolokaforge.user_simulators``
    entry-point group.
    """

    last_system_prompt: str | None


@dataclass(frozen=True)
class UserSimulatorContext:
    """The trial-scoped inputs a user-simulator factory receives.

    The conductor builds one per conversational trial from the resolved
    ``actors.user`` config. ``mode``, ``persona``, ``backstory`` and
    ``scripted_flow`` are the engine's built-in simulator fields; ``llm_config``,
    ``tool_schemas`` and ``rate_limit_probe`` are the trial dependencies the
    built-in simulator needs. ``tool_turns`` is the actor's
    ``actors.user.tool_turns``: under ``isolated`` the runner records the
    simulator's tool calls as steps the agent never reads, and a simulator
    builds its request from
    :func:`~tolokaforge.core.actors.tool_turns.simulator_view` so it sees its
    own steps; under ``shared`` (the default) from
    :func:`~tolokaforge.core.actors.tool_turns.shared_view`.

    ``simulator_config`` is the escape hatch a non-built-in simulator reads its
    own configuration from: the engine passes ``actors.user.simulator_config``
    through verbatim and never interprets its keys, so a benchmark simulator
    declares its own fields there and validates them into its own model. The
    built-in simulator ignores it.
    """

    mode: str
    persona: str
    backstory: str | None
    scripted_flow: list[dict[str, str]] | None
    tool_schemas: list[dict[str, Any]] | None
    llm_config: ModelConfig | None = None
    rate_limit_probe: RateLimitProbeConfig | None = None
    tool_turns: UserToolTurns = "shared"
    simulator_config: dict[str, Any] = field(default_factory=dict)


UserSimulatorFactory = Callable[[UserSimulatorContext], UserSimulator]
