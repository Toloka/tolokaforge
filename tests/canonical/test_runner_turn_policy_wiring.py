"""Runner ↔ ``TurnPolicy`` wiring lock (Stage 5 of the multi-actor design).

The runner today constructs a :class:`ToolCallingLoop` whose optional
``user_turn`` seam is a shim that delegates to
``policy.next_actor(...)``. Two invariants this file locks:

* Under :class:`~tolokaforge.core.models.task_config.TaskConfig.interaction_mode`
  ``"conversational"`` (the default), the runner resolves
  :class:`~tolokaforge.core.actors.turn_policy.ConversationalTurnPolicy`
  through the ``tolokaforge.turn_policies`` entry-point registry and
  dispatches the user actor after every tool-call-free agent turn —
  byte-for-byte the historical two-party shape.
* Under ``"agent_only"`` the runner resolves
  :class:`~tolokaforge.core.actors.turn_policy.AgentOnlyTurnPolicy`, whose
  :meth:`next_actor` terminates the trial as
  :attr:`~tolokaforge.core.models.TerminationReason.AGENT_DONE` the first
  time the agent takes a turn without calling a tool: no user turn is ever
  dispatched, and the simulator (if the caller happens to pass one) is never
  consulted. The conductor passes ``user_simulator=None`` under this mode, so
  the never-called invariant is the design guarantee agent-monologue tasks
  rely on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm.client import GenerationResult, UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import Message, TerminationReason
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical


class _ScriptedAgent:
    """Agent generate seam yielding one queued item per turn, repeating the last."""

    def __init__(self, *items: GenerationResult) -> None:
        self._items = list(items)

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        return self._items.pop(0) if len(self._items) > 1 else self._items[0]

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _agent(text: str) -> GenerationResult:
    return GenerationResult(
        text=text, tool_calls=[], usage=Usage(prompt_tokens=10, completion_tokens=5)
    )


def _spy_simulator() -> MagicMock:
    """Simulator spy that would return ``###STOP###`` if consulted.

    ``reply`` returning a stop-token would end the dialogue with
    ``USER_STOP``, which is a distinguishable outcome from
    ``AGENT_DONE`` — so the ``call_count == 0`` invariant is checked
    against a reply the runner *cannot silently discard*.
    """
    sim = MagicMock(spec=UserSimulator)
    sim.reply.return_value = GenerationResult(text="###STOP###", tool_calls=[], usage=Usage())
    sim.last_system_prompt = None
    return sim


def test_agent_only_mode_never_invokes_user_simulator() -> None:
    """The whole trial completes without a single simulator invocation.

    The scripted agent has three no-tool-call turns queued and only the first is
    ever consumed: the policy terminates on it, so turns two and three never
    generate. Under the conversational shape that same first turn would fire the
    loop's ``user_turn`` seam, and the seed turn would already have dispatched
    the simulator once before it.
    """
    simulator = _spy_simulator()
    agent = _ScriptedAgent(
        _agent("Reading the workdir."),
        _agent("Running the tests."),
        _agent("All green."),
    )

    trajectory = TrialRunner(
        task_id="agent-only-wiring",
        trial_index=0,
        agent_client=agent,
        user_simulator=simulator,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=10,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
    ).run("You are an agent.", "Migrate the crate to Rust.")

    assert simulator.reply.call_count == 0, (
        "AgentOnlyTurnPolicy dispatched the user simulator; the whole point "
        "of the mode is that the seam is neutralised for agent-monologue tasks."
    )
    assert trajectory.termination_reason is TerminationReason.AGENT_DONE, (
        "an agent-only trial whose agent takes a turn without calling a tool must "
        f"terminate as AGENT_DONE; got {trajectory.termination_reason!r}"
    )


def test_agent_only_mode_accepts_none_user_simulator() -> None:
    """The conductor passes ``user_simulator=None`` under agent-only; the
    runner must complete a trial anyway.

    Locks the design guarantee that agent-monologue tasks never
    construct a simulator: neither the bootstrap nor the turn seam
    reaches into ``self.user_simulator``.
    """
    trajectory = TrialRunner(
        task_id="agent-only-no-sim",
        trial_index=0,
        agent_client=_ScriptedAgent(_agent("Done.")),
        user_simulator=None,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
    ).run("You are an agent.", "Migrate the crate to Rust.")

    assert trajectory.termination_reason is TerminationReason.AGENT_DONE


def test_agent_only_mode_text_only_completion_terminates_as_agent_done() -> None:
    """Regression lock for #876 at the runner-wiring level.

    The real-MB smoke that surfaced #876 died on this exact shape: the
    agent finished the migration and emitted a text-only completion
    summary, no tool calls. Under the pre-fix implementation the runner
    shim returned an empty ``UserTurnResult`` and the loop retried the
    agent — sending a request ending in ``role: assistant`` which
    Anthropic's ``opus-4-6`` rejects as unsupported prefill, so the trial
    went to ``status=error`` and ``/grade`` was skipped entirely.

    Post-fix, ``AgentOnlyTurnPolicy.next_actor`` returns a
    :class:`TerminationDecision` with ``reason=AGENT_DONE``. The runner
    shim propagates it via ``UserTurnResult(termination=...)`` and the
    loop honors it: the trial completes with ``AGENT_DONE`` and the
    downstream grading path fires. This test locks that end-to-end
    wiring, not just the policy's return value (which is unit-tested
    separately in ``test_turn_policy_contract.py``).
    """
    simulator = _spy_simulator()  # never invoked
    agent = _ScriptedAgent(
        _agent("The migration is complete. All tests pass. Migrated 20 subcommands to Go.")
    )

    trajectory = TrialRunner(
        task_id="agent-only-text-only-completion",
        trial_index=0,
        agent_client=agent,
        user_simulator=simulator,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=10,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
    ).run("You are a migration agent.", "Migrate the crate to Rust.")

    assert simulator.reply.call_count == 0, (
        "AgentOnlyTurnPolicy must not dispatch the simulator even on the "
        "text-only-completion branch."
    )
    assert trajectory.termination_reason is TerminationReason.AGENT_DONE, (
        f"text-only agent completion under agent_only must route to AGENT_DONE; "
        f"got {trajectory.termination_reason!r} (pre-#876 shape would have been ERROR)."
    )


def test_agent_only_mode_without_initial_message_fails_loud() -> None:
    """The ``AgentOnlyTurnPolicy.bootstrap`` contract raises when neither
    an ``initial_user_message`` nor a live simulator can seed turn 0 —
    surfaced at run-start rather than degraded into a silent empty
    user turn."""
    trajectory = TrialRunner(
        task_id="agent-only-missing-seed",
        trial_index=0,
        agent_client=_ScriptedAgent(_agent("unreached")),
        user_simulator=None,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
    ).run("You are an agent.", "")

    # The runner catches init errors and produces an ERROR trajectory —
    # the operator sees the ValueError text in the trailing system
    # message rather than a silent no-op trial.
    assert trajectory.termination_reason is TerminationReason.ERROR
    error_surfaced = any("agent_only" in (m.content or "") for m in trajectory.messages)
    assert error_surfaced, "ValueError text must land in the trajectory system message"


def test_conversational_mode_dispatches_user_simulator() -> None:
    """Byte-for-byte parity check: the default (``conversational``) still
    routes every tool-call-free agent turn through the user simulator.

    The scripted agent produces one no-tool-call turn; the simulator's
    ``###STOP###`` closes the dialogue with ``USER_STOP`` — a code path
    only reachable by an actual simulator dispatch.
    """
    simulator = _spy_simulator()
    trajectory = TrialRunner(
        task_id="conversational-parity",
        trial_index=0,
        agent_client=_ScriptedAgent(_agent("Anything else?")),
        user_simulator=simulator,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="conversational",
    ).run("You are an agent.", "Please do the task.")

    assert simulator.reply.call_count == 1
    assert trajectory.termination_reason is TerminationReason.USER_STOP
