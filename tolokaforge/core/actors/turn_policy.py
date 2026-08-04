"""Turn-policy Protocol, value objects, and the built-in conversational policy.

A :class:`TurnPolicy` is the *choreographer* of the turn loop: it owns the two
decisions the loop body used to hard-code — how message index 0 is delivered
(:meth:`~TurnPolicy.bootstrap`) and which actor speaks next after each agent
turn (:meth:`~TurnPolicy.next_actor`, or ``None`` to end the turn cycle).
Lifting these into a Protocol lets the two-party conversational shape and the
agent-monologue shape share the same loop body and diverge only at the seam.

The Protocol follows the ADR-0026 *Service Readiness Contract* Pattern-A
shape and mirrors the sibling :class:`~tolokaforge.core.actors.actor.Actor`
seam:

* ``@runtime_checkable`` so seam wiring can ``isinstance``-check.
* Frozen value objects at the boundary (:class:`BootstrapDecision`,
  :class:`TurnState`, :class:`ActorTurn`) so future actor kinds add fields
  with defaults without breaking existing policies.
* No construction-time deps on the Protocol itself; concrete impls own
  the actors they hand back.

The built-in :class:`ConversationalTurnPolicy` reproduces the two-party
turn loop the runner has always driven: user simulator provides the seed
message when the caller does not, and the user is dispatched after each
agent turn that emits no tool calls. Registered via the
``tolokaforge.turn_policies`` entry-point group and looked up by
``TaskConfig.interaction_mode``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tolokaforge.core.actors.actor import Actor, Message

if TYPE_CHECKING:
    from tolokaforge.core.models.task_config import TaskConfig
    from tolokaforge.core.plugin_registry import TurnPolicyContext

__all__ = [
    "ActorTurn",
    "BootstrapDecision",
    "ConversationalTurnPolicy",
    "TurnPolicy",
    "TurnState",
]


@dataclass(frozen=True)
class BootstrapDecision:
    """How message index 0 should be delivered.

    ``first_user_message`` carries the literal turn-0 user text when the
    caller supplied it; ``None`` when the runner must consult
    ``bootstrap_via_simulator`` and dispatch a user simulator to produce it.
    """

    first_user_message: str | None
    bootstrap_via_simulator: bool


@dataclass(frozen=True)
class TurnState:
    """Loop state a policy needs to pick the next actor.

    ``last_agent_had_tool_calls`` drives the conversational policy's
    user-dispatch decision — a tool-call turn stays with the agent so the
    tool results feed the next agent generation instead of a synthetic
    user reply.
    """

    messages: list[Message]
    last_agent_had_tool_calls: bool
    turn_index: int


@dataclass(frozen=True)
class ActorTurn:
    """The actor the policy hands back for the next turn.

    ``actor_name`` is the seam-friendly key the runner uses for
    observation identity (e.g. ``"user"``); ``actor`` is the concrete
    :class:`Actor` instance the runner dispatches.
    """

    actor_name: str
    actor: Actor


@runtime_checkable
class TurnPolicy(Protocol):
    """Choreograph the turn loop.

    :meth:`bootstrap` decides how message index 0 is delivered — a
    caller-provided literal, a live simulator dispatch, or a synthetic
    seed. :meth:`next_actor` picks the actor to speak next given the loop
    state, or returns ``None`` to end the turn cycle (the agent monologue
    case).
    """

    def bootstrap(self, task: TaskConfig, initial_user_message: str | None) -> BootstrapDecision:
        """Return the turn-0 delivery decision for ``task``."""
        ...

    def next_actor(self, state: TurnState) -> ActorTurn | None:
        """Return the actor to speak next, or ``None`` to end the turn cycle."""
        ...


class ConversationalTurnPolicy:
    """Two-party turn loop: user simulator seeds turn 0 and speaks after
    every tool-call-free agent turn.

    ``bootstrap`` short-circuits to the caller's ``initial_user_message``
    when it carries non-whitespace text; otherwise routes to the user
    simulator (``bootstrap_via_simulator=True``). ``next_actor`` returns
    the user actor when the previous agent turn produced no tool calls,
    matching the loop's historical ``_advance_user_turn`` gate.
    """

    def __init__(self, *, user_simulator: Actor) -> None:
        self._user_simulator = user_simulator

    def bootstrap(self, task: TaskConfig, initial_user_message: str | None) -> BootstrapDecision:
        if initial_user_message is not None and initial_user_message.strip():
            return BootstrapDecision(
                first_user_message=initial_user_message,
                bootstrap_via_simulator=False,
            )
        return BootstrapDecision(
            first_user_message=None,
            bootstrap_via_simulator=True,
        )

    def next_actor(self, state: TurnState) -> ActorTurn | None:
        if state.last_agent_had_tool_calls:
            return None
        return ActorTurn(actor_name="user", actor=self._user_simulator)


def _conversational_policy_factory(context: TurnPolicyContext) -> ConversationalTurnPolicy:
    """Build a :class:`ConversationalTurnPolicy` from a resolved context.

    Fails loud if the runner did not supply a user simulator — the
    conversational shape cannot function without one, and a silent fallback
    would just re-hide the mismatch this seam was introduced to make visible.
    """
    if context.user_simulator is None:
        raise ValueError(
            "ConversationalTurnPolicy requires TurnPolicyContext.user_simulator; "
            "route agent-monologue tasks through the agent_only policy instead."
        )
    return ConversationalTurnPolicy(user_simulator=context.user_simulator)
