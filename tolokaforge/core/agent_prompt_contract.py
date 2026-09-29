"""Named reply contracts for an agent working a task on its own.

A contract is the part of an agent system prompt that says *how to reply* —
distinct from the task's domain policy, which says what the work is. It is
data, rendered by a pure function; there is no seam here and no Protocol
(ADR-0011 § "Do not introduce a Protocol when the component is a pure
data-transformation utility").

Selection is by name from :data:`CONTRACTS`, or by path when a pack ships its
own. :func:`resolve_agent_prompt_contract` returns the text; composition with a
task's own policy document belongs to
:func:`~tolokaforge.core.system_prompt.build_system_prompt`.

``reasoning_agent`` is written for the shape a terminal benchmark has: one
shell tool, no user to talk to, a grader that reads the container and never the
transcript. Under that shape a model gets no feedback for thinking aloud, and
some models stop doing it — measured at 5 of 2,239 assistant messages for one
model, against 100% of 2,333 for the same model under a harness whose contract
requires prose. The scores were 0.32 and 0.83.

Five properties are deliberate, and each departs from the external harness the
behaviour was first observed in:

* **Prose and the tool call ride the same turn.** That harness parses free text
  and so needs a JSON envelope the model must emit correctly; its own corpus
  carries steps whose only observation is a parse error, plus a repair loop. We
  call tools natively, so the envelope buys nothing and costs a failure mode.
  The contract asks for reasoning in the message body *beside* a real tool
  call.
* **It asks three questions, not for "reasoning".** Reconciling the last output
  against expectation, stating what remains, and predicting what the next
  command will produce keep the note from decaying into a restatement of
  intent, and let the next turn falsify the prediction. In that harness's own
  corpus its equivalent prose is the only deliberation present on roughly 61%
  of turns — the separate reasoning channel is empty on the rest — so this text
  does the work rather than decorating it.
* **It gives the reason, not just the rule.** A model told only to narrate has
  no stake in it. A model told that its terminal output is not kept for it has
  one.
* **A bare prose turn is fenced off explicitly.** Under
  ``interaction_mode: agent_only`` a turn with no tool call ends the episode
  immediately — at any index, turn 1 included, with status ``COMPLETED`` and
  normal grading against an untouched container. A contract that asks for
  reasoning without saying this invites a model to open with a plan and score
  zero for it. The instruction never to send a lone message while working is
  load-bearing, not politeness.
* **Finishing is gated, not merely permitted.** Completion is structural — a
  turn carrying no tool call, which
  :class:`~tolokaforge.core.actors.turn_policy.AgentOnlyTurnPolicy` already
  treats as done (ADR-0032), and no sentinel token, which
  ``tests/canonical/test_agent_prompt_exit_token.py`` holds. In that harness
  agents assert completion 84 times across 50 trials and 45 of those are not
  final, so the contract asks for a verification pass before the claim.

Two things it deliberately omits. There is no instruction to batch commands:
that harness averages 2.35 shell commands a turn against our 1.24, which is
most of why it needs 46.7 turns where we need 79.7, but its prompt never asks
for it — the batching falls out of a schema taking an array of commands. It is
a tool-shape change, and asking for it in prose would be cargo cult. And there
is no echo of the engine's legacy customer-service instruction, which tells an
agent it may send a message **or** call a tool and "cannot do both at the same
time" — right when a user is waiting for the floor, and exactly backwards for
an agent working alone.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "CONTRACTS",
    "DEFAULT_SOLO_CONTRACT",
    "UnknownAgentPromptContractError",
    "resolve_agent_prompt_contract",
]


#: Bumped when the text of any shipped contract changes. A canonical test pins
#: the rendered bytes against this counter so an edit is deliberate and shows
#: up in review as a number, not only as prose.
GENERATION = 1


_REASONING_AGENT = """You are an expert software engineer working on your own inside a Linux container.

Every turn, before you act, write a short note in your message covering:
- what the last output actually showed
- what is done now, and what is still left
- what you are about to run, and what you expect it to produce

Then make the tool call in the same turn. The note and the tool call belong together; you \
are not choosing between them.

Never send a message on its own while you are still working — a message with no tool call \
is how you end the task, so a turn spent only thinking will stop you before you have \
started.

Keep your own running picture of the problem. Command output scrolls away and is not kept \
for you: whatever you do not write down, you will have to work out again. Each turn, start \
by checking what actually happened against what you expected.

Before you finish, check your work the way the project would — run its tests or its own \
checks if it has them.

When the task is done, reply with a short summary of what you changed and make no tool \
call. Do that only once you have verified the work; if you are not sure it is finished, \
keep going."""


#: Shipped contracts by name. Values are the verbatim text a run receives.
CONTRACTS: dict[str, str] = {
    "reasoning_agent": _REASONING_AGENT,
}

#: The contract a solo agent gets when a model preset asks for one by default.
DEFAULT_SOLO_CONTRACT = "reasoning_agent"


class UnknownAgentPromptContractError(ValueError):
    """A contract was selected by a name no shipped contract answers to."""


def resolve_agent_prompt_contract(selector: str, *, task_dir: Path) -> str:
    """Return the contract text *selector* names.

    A bare name is looked up in :data:`CONTRACTS`. Anything else is read as a
    path relative to *task_dir*, matching how ``TaskConfig.system_prompt``
    resolves, so a pack can ship a contract of its own beside its tasks.

    Raises :class:`UnknownAgentPromptContractError` when a name matches no
    shipped contract and names no readable file — a silently ignored selector
    would run the whole task set on the wrong prompt.
    """
    if selector in CONTRACTS:
        return CONTRACTS[selector]

    candidate = Path(selector)
    path = candidate if candidate.is_absolute() else task_dir / candidate
    if path.is_file():
        text = path.read_text()
        if not text.strip():
            raise UnknownAgentPromptContractError(
                f"agent prompt contract file {path} is empty; omit the selector "
                f"to use the task's own prompt"
            )
        return text

    known = ", ".join(sorted(CONTRACTS))
    raise UnknownAgentPromptContractError(
        f"unknown agent prompt contract {selector!r}: it is not a shipped "
        f"contract ({known}) and no file exists at {path}"
    )
