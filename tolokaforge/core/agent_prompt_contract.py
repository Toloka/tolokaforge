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

Six properties are deliberate, and each departs from the external harness the
behaviour was first observed in:

* **The note and the tool call ride the same turn.** That harness parses free
  text and so needs a JSON envelope the model must emit correctly; its own
  corpus carries steps whose only observation is a parse error, plus a repair
  loop. We call tools natively, so the envelope buys nothing and costs a
  failure mode. The contract asks for the note *beside* a real tool call.
* **The note has a structural slot when the tool offers one.** A tool whose
  schema carries a required ``note`` string (``bash_batch`` does) takes the
  note as an argument: models fill required arguments far more reliably than
  they follow a standing instruction, which the same model honoured on 47% of
  turns. The slot is declared, not enforced — the runner does not validate a
  docker-exec call's arguments against its schema, so a call without a note
  still runs its commands. The contract routes the note there when the slot
  exists and into the message otherwise, so one text serves a single-command
  tool and a batching one.
* **It says what the note is not.** A task may forbid a separate planning
  step, and the sentence that does so does not obviously exempt a note
  riding the message that carries the next command. The distinction is
  drawn here rather than left to the reader.
* **It asks three questions, not for "reasoning".** Reconciling the last output
  against expectation, stating what remains, and predicting what the next
  command will produce keep the note from decaying into a restatement of
  intent, and let the next turn falsify the prediction.
* **It describes what the loop keeps only when the loop drops something.** The
  shipped text says nothing about how long command output stays in the
  conversation, because by default it stays: every observation is replayed in
  full on every call. When a model's ``observation_window`` is set the loop
  collapses older ``role=tool`` messages to a line naming how much was dropped,
  and :func:`~tolokaforge.core.system_prompt.build_system_prompt` appends
  :func:`observation_window_clause` to whichever contract is in force — shipped
  or pack-supplied — so the model is told the number and the consequence. A
  statement about retention that the configuration does not make true is not
  in the contract.
* **A bare prose turn is fenced off explicitly.** Under
  ``interaction_mode: agent_only`` a turn with no tool call ends the episode
  immediately — at any index, turn 1 included, with status ``COMPLETED`` and
  normal grading against an untouched container. A contract that asks for
  reasoning without saying this invites a model to open with a plan and score
  zero for it. The instruction never to send a lone message while working is
  load-bearing, not politeness.
* **Finishing is gated, not merely permitted, and carries no report.**
  Completion is structural — a turn carrying no tool call, which
  :class:`~tolokaforge.core.actors.turn_policy.AgentOnlyTurnPolicy` already
  treats as done (ADR-0032), and no sentinel token, which
  ``tests/canonical/test_agent_prompt_exit_token.py`` holds. In that harness
  agents assert completion 84 times across 50 trials and 45 of those are not
  final, so the contract asks for a verification pass before the claim. It asks
  for no closing summary: the grader reads the container, so prose on the final
  turn is paid for at the trial's largest prompt and read by nobody.

One thing it does not solve. The contract opens with a persona, and so does
any pack document that assigns one. ``build_system_prompt`` drops only the
engine's own generic persona; a task that ships its own ``system_prompt`` file
keeps it verbatim, because rewriting an author's document is not this layer's
business. Selecting a contract for such a task therefore yields two personas,
and the contract's is the wrong one for it. The combination is legitimate but
the caller owns the collision — a contract is for a task the agent works alone
on, which is not the shape a persona-bearing pack usually describes.

Two things it deliberately omits. It does not say how many commands to send a
turn: that is a property of the tool's shape, and ``bash_batch`` states its own
batching rule in its schema description, where the model reads it beside the
``commands`` array it applies to. And there is no echo of the engine's legacy
customer-service instruction, which tells an agent it may send a message **or**
call a tool and "cannot do both at the same time" — right when a user is
waiting for the floor, and exactly backwards for an agent working alone.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "CONTRACTS",
    "DEFAULT_SOLO_CONTRACT",
    "OBSERVATION_WINDOW_CLAUSE",
    "UnknownAgentPromptContractError",
    "observation_window_clause",
    "resolve_agent_prompt_contract",
]


#: Bumped when the text of any shipped contract changes. A canonical test pins
#: the rendered bytes against this counter so an edit is deliberate and shows
#: up in review as a number, not only as prose.
GENERATION = 4


_REASONING_AGENT = """You are an expert software engineer working on your own inside a Linux container.

Every turn, make a tool call and write a note in the same turn — in the tool's `note` \
argument when it has one, otherwise in your message. The note says what the last output \
actually showed, what is done and what is still left, and what you expect the next command \
to produce. A task that tells you not to write a plan is not telling you to stop writing \
these.

Never send a message on its own while you are still working — a message with no tool call \
is how you end the task, so a turn spent only thinking will stop you before you have \
started.

Keep your own running picture of the problem, and start each turn by checking what \
actually happened against what you expected.

Before you finish, check your work the way the project would — run its tests or its own \
checks if it has them.

When the task is done and you have verified it, reply with no tool call. If you are not \
sure it is finished, keep going."""


#: Appended to the contract in force when the loop's ``observation_window`` is
#: set. ``{kept}`` is the window. Rendered by :func:`observation_window_clause`.
OBSERVATION_WINDOW_CLAUSE = """Only your {kept} most recent command outputs stay in your history. Each older one is \
replaced by a line saying how many characters were dropped, so whatever you still need from \
an output, write it in your note before it goes."""

#: The same clause for a window of zero, where no output outlives its turn.
_OBSERVATION_WINDOW_ZERO_CLAUSE = """Command outputs do not stay in your history: each one is replaced by a line saying how \
many characters were dropped, so whatever you need from an output, write it in your note."""


def observation_window_clause(window: int) -> str:
    """The sentence a contract carries when only *window* observations stay on the wire.

    Composed by :func:`~tolokaforge.core.system_prompt.build_system_prompt`
    when the model's ``observation_window`` is not ``None``, after whichever
    contract is in force, so a pack-supplied contract is told the same truth a
    shipped one is. With no window nothing is appended: the full history is
    replayed and there is nothing to say.
    """
    if window < 0:
        raise ValueError(f"observation_window must be non-negative, got {window}")
    if window == 0:
        return _OBSERVATION_WINDOW_ZERO_CLAUSE
    return OBSERVATION_WINDOW_CLAUSE.format(kept=window)


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
