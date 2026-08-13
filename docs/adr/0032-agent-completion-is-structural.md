# 0032. The agent's completion is structural; `###STOP###` is the user simulator's

- **Status:** Accepted
- **Date:** 2026-08-13
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Revises:** [ADR-0028](0028-multi-actor-turn-policy.md) — its § Decision claim that
  `AgentOnlyTurnPolicy.next_actor` returns `None` unconditionally (already refined for
  #876 in that ADR's own text) and its § "Stop protocol reuses the existing marker",
  which made `_AGENT_DONE_MARKERS` serve both interaction modes.

## Context and Problem Statement

`TrialRunner` terminated a trial as `TerminationReason.AGENT_DONE` whenever an agent
turn's text contained `###STOP###`, matched case-insensitively as a bare substring.
`###STOP###` is the **user simulator's** exit token: it is authored into simulator
backstories and scripted flows, and the simulator's own prompt rules instruct it.
Measured against the tree: every one of the 64 in-repo YAML files carrying the token
carries it in a simulator backstory, a scripted flow, or a recorded trial fixture, and
driving `build_system_prompt` over every example pack that loads — 29 of the 31 task
files; the two terminal-bench descriptors declare no `task_id` — produces no agent
prompt containing it.

An agent therefore met that check only by coincidence — most plausibly by quoting the
instruction back at the user ("reply with `###STOP###` and I'll close the ticket"),
which ended the trial the agent was still working. Because the termination policy runs
before tool execution, a tool call sharing that turn was dropped unexecuted and
unrecorded. Under `agent_only` the marker contributed nothing either:
`AgentOnlyTurnPolicy` already terminates on the first tool-call-free agent turn, which
is strictly earlier than any marker could fire.

So the enum member documented as "agent signalled task completion" described something
no shipped configuration could produce, while the code claiming to produce it ended
trials the agent did not mean to end.

## Decision Drivers

- **A signal the agent is never told about is not a signal.** Either every agent-facing
  prompt instructs the marker, or nothing may be checked against it. Three of
  `build_system_prompt`'s five branches return author text verbatim, so the harness
  cannot guarantee any instruction reaches the agent.
- **One token, one owner.** The simulator's exit condition and the agent's completion
  are different questions; sharing a token conflates them and is what produced this.
- **A completion signal must not be forgeable by quoting**, and must not cost the trial
  a tool call.
- **Grading needs the distinction to mean something.** `agent_done` versus `max_turns`
  is a claim about how a trial ended, and the number that carries it is published.

## Considered Options

1. **Instruct the marker in agent prompts and keep the check** — the issue's own
   proposal. Rejected: it cannot reach author-supplied prompts, and a token an agent is
   asked to emit is one it can forget or emit spuriously, so the signal stays
   unreliable in the other direction.
2. **A completion tool** (the `submit_report` shape the rubric judge already uses).
   Coherent, and the only way an agent could deliberately end a two-party dialogue. Not
   taken here: it enlarges the agent's action space on every shipped task and interacts
   with trace-check call budgets and activity bounds (#677). It is its own ticket.
3. **Decide completion structurally, and delete the marker check.** **This ADR.**

## Decision

We adopt **Option 3**. `TerminationReason.AGENT_DONE` means exactly one thing:

> The agent had no further action to take, and no counterparty could ask it for more.

That condition is decided by the component that owns turn choreography, never by
reading assistant prose. It has exactly two producers:

- `AgentOnlyTurnPolicy.next_actor` — the agent took a turn with no tool calls, and the
  shape has no user party. Definitional, and already implemented.
- `_SubmitReportTermination` in `tolokaforge/core/grading/judge.py` — the judge called
  `submit_report`, an explicit tool call.

`TrialRunner._is_done` and `_AGENT_DONE_MARKERS` are deleted; the agent's termination
policy keeps stuck detection and returns `None` otherwise. Under `conversational` the
condition is never true — a two-party dialogue is closed by the user, which is
`USER_STOP` — so `AGENT_DONE` is not producible there, by design.

`###STOP###` stays exactly what it is: a user-simulator construct, read only from
simulator replies, documented as such in [`docs/TASKS.md`](../TASKS.md) and guarded by
`tests/canonical/test_agent_prompt_exit_token.py`, which builds every example pack's
agent prompt and fails if any carries the token.

## Consequences

### Positive

- The signal cannot be forged by quoting, cannot be missed for want of an instruction,
  and cannot swallow a tool call.
- `AGENT_DONE`'s two producers are both structural or explicit, so the enum member's
  documented meaning is the one the code produces.
- The turn policy is the single owner of "who speaks next, and whether anyone can".

### Negative / Trade-offs

- **A conversational agent has no way to declare itself finished.** It is the user
  simulator that ends the dialogue, or the turn budget. This is deliberate (Option 2 is
  the answer if the capability is wanted), and it means one of #708's acceptance
  criteria is knowingly unmet.
- **Conversational difficulty is re-baselined.** A trial that used to end early on a
  coincidental token match now runs on, taking more turns and more tokens; it ends
  `user_stop` or `max_turns`, both already graded.
- **`agent_only` still calls a do-nothing trial `AGENT_DONE`.** An agent whose first
  turn is text-only terminates with zero tool calls. `AGENT_DONE` remains truthful
  about "no further action" and silent about "any action at all"; the activity lower
  bound is #677's.

### Follow-ups

- Code changes required: done in #708 — the deletion, the docstrings that named the
  marker as an agent termination path, and `TerminationReason.AGENT_DONE`'s comment.
- Documentation to update: done in #708 — `docs/CONFIG.md` § `interaction_mode:`,
  `docs/GRADING.md` § "How the trial ended", `docs/TASKS.md` § the exit condition.
- Tests to add: done in #708 — two unit locks through `TrialRunner`, the enum
  reachability driver retargeted to `agent_only`, an assertion on the judge's producer,
  and the corpus prompt guard above.

## Links

- Related ADRs: [ADR-0028](0028-multi-actor-turn-policy.md) (revised in part, see
  above), [ADR-0020](0020-judge-protocol.md) (the judge seam whose `submit_report`
  produces the other `AGENT_DONE`).
- Related code: `tolokaforge/core/actors/turn_policy.py`,
  `tolokaforge/core/runner.py`, `tolokaforge/core/grading/judge.py`.
- External references: #708, #876 (the structural path this decision leaves as the only
  producer), #677 (activity lower bound).
