# 0033. Whether an agent must act is a per-task assertion, not a stuck heuristic

- **Status:** Accepted
- **Date:** 2026-08-13
- **Deciders:** @azorej
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`StuckDetector._has_idle_turns` claimed to notice an agent that had stopped making
progress: assistant turns carrying no tool calls. With `max_idle_turns = N` it took the
last `N` **messages**, counted the assistant messages among them that carried no tool
calls, and fired when that count reached `N`. `ToolCallingLoop` appends a `user` message
or one or more `role: tool` messages after every assistant turn, so that window is at
best half assistant and the condition is unsatisfiable for every `N >= 2`. Driven
through `TrialRunner` with an agent that never calls a tool, over 20 turns, the ceiling
is exactly `floor(N / 2)`:

| `max_idle_turns` | termination | max `idle_count` observed |
|---|---|---|
| 1 | `stuck_detected` (first assistant turn, 3 messages) | 1 |
| 2 | `max_turns` | 1 |
| 3 | `max_turns` | 2 |
| 8 | `max_turns` | 4 |
| 12 | `max_turns` | 6 |

No shipped threshold was 1. The conductor reads the task-scope block when the task
declares one and falls back to the run-side `OrchestratorConfig.stuck_heuristics`
otherwise, so **12 — the run-side default — was the effective threshold for every task
with no task-scope block**, which is every shipped pack but one. The task-scope model
default of 3 applied only to a block that declared other knobs and omitted this one; the
two values declared anywhere in the tree were 3 (`example-microservices-pack`) and 2
(`tau_retail_mini`); the docs advertised 8. The heuristic was dead at all five, and the
only construction that ever reached it was a synthetic `max_idle_turns=1` in the
termination-reason reachability test.

So a scoring-relevant control — a stuck verdict ends the trial and short-circuits its
grade to `0.0` without its state being checked — read as live in two config models, in
three documents and in the detector, and answered nothing.

## Decision Drivers

- **A control that cannot fire is worse than an absent one.** It is configured,
  documented and believed, and the gap it was supposed to cover is uncovered.
- **The repair is worse than the defect.** Counting the last `N` *assistant* turns makes
  the heuristic fire on "N consecutive tool-call-free assistant turns". Measured against
  the 34 recorded trajectories in `tests/data/` — real model runs, all ended normally
  (`user_stop` / `max_turns`), none stuck, every one graded:

  | threshold | trials that would terminate `stuck_detected` | their current scores |
  |---|---|---|
  | 2 (shipped in `tau_retail_mini.yaml`) | **23 / 34** | every one > 0; thirteen exactly `1.0`; the rest 0.395–0.9 |
  | 3 (shipped in the example project) | 2 / 34 | both `1.0` |
  | 4 and above | 0 / 34 | — |

  The pattern it catches is not stalling. It is the ordinary end of a conversational
  task: the agent finishes acting, says what it did, the user says thank you, the agent
  says you're welcome, the user sends `###STOP###`. Two consecutive prose turns is how a
  polite dialogue closes.
- **Prose without tool calls is a per-task question.** `docs/TASKS.md` § "Refusal tasks
  and other do-nothing passes" documents tasks whose *correct* behaviour is to act on
  nothing. A harness-global rule that auto-fails an agent for talking is task-specific
  policy compiled into the harness, which Core Rule 2 forbids.
- **The harness already has the place to ask it.** `transcript_rules.min_assistant_turns`
  and `transcript_rules.tool_expectations.required_tools` are opt-in, per-task,
  grading-side, and counted in assistant turns.

## Considered Options

1. **Repair the units** — filter the window to assistant messages, or size it to
   `2 * max_idle_turns`. This is #709's own proposal. Rejected on the corpus measurement
   above: at the thresholds this tree ships it converts 68 % of known-good trials into
   `0.0` auto-fails whose state is never checked.
2. **Repair the units and raise the threshold** past what the corpus reaches. The
   heuristic then asserts a shape nothing in the corpus reaches, which is the defect
   being fixed, one number over.
3. **Delete the heuristic and leave the question to `transcript_rules`.** **This ADR.**

## Decision

We adopt **Option 3**. `StuckDetector` runs two heuristics — repeated identical tool
calls, and repeated content — and `is_stuck` means the agent is repeating itself.
`_has_idle_turns`, the `max_idle_turns` constructor parameter, and the
`stuck_heuristics.max_idle_turns` key on both config models are deleted. A config still
declaring the key loads and emits a `DeprecationWarning` naming the file, this ADR, and
the `transcript_rules` replacement: both models ignore unknown keys, so removing the
field alone would drop an author's declaration without a word.

Whether an agent must *act* is declared per task in `grading.yaml transcript_rules`,
where the task author scopes it to the tasks it is true of.

That every surviving heuristic is satisfiable at every threshold this repository ships,
given a turn budget that admits it, is now enforced by
`tests/canonical/test_stuck_heuristics_are_satisfiable.py`, which discovers both the
heuristic set and the threshold set rather than listing them, and drives each case
through a real trial at a budget manufactured to admit the threshold. It does not claim
a shipped pack reaches those conditions inside its own turn budget — see § Still
unaddressed.

## Consequences

### Positive

- Every heuristic the detector runs is satisfiable at every shipped threshold, and a
  heuristic added without a driving case fails a test.
- The question the deleted heuristic gestured at is asked where it can be answered
  truthfully — per task, by the author who knows whether acting is required.

### Negative / Trade-offs

- **No configuration in this repository changes a verdict**, so this is not a
  re-baselining: the heuristic fired only at `max_idle_turns = 1`, and nothing in the
  tree declares 1. Recorded bundles and replay are byte-unchanged;
  `TerminationReason.STUCK_DETECTED`, `Metrics.stuck_detected` and the aggregate's
  `stuck_rate` keep their shape, their wire encoding and their grading routing.
- **An external pack declaring `max_idle_turns: 1` loses a trigger** that ended the trial
  on the agent's first tool-call-free turn. No such pack is known. The deprecation
  warning names the replacement.
- **An agent that talks without acting still runs to `max_turns`.** That is the intended
  end state — it is graded, and the state it left is checked — but a pack that wants it
  to fail must say so in `transcript_rules`.

### Still unaddressed

- **The reasoning-only stall (#1142).** `docs/GEMINI_QUIRKS.md` § 3.1 records Gemini
  Flash 3.5 producing ≥3 consecutive max-tokens reasoning-only calls in production. That
  is the real "agent stopped making progress" shape, it is typed evidence
  (`finish_reason`, reasoning-only output) rather than a turn count, and nothing detects
  it. It is the honest successor to the heuristic deleted here.
- **`_has_looping_content` sees only intra-message repetition (#1141).** It fires on a
  phrase repeated inside one message and never on repetition across turns, and it returns
  `False` before reading any content once the agent emits two or more tool messages per
  turn.
- **`max_repeated_tool_calls` still has two live answers (#1145)**: 5 on the task-scope
  `StuckHeuristicsDefaults` and 10 on the run-side `StuckHeuristics`, both readable on
  the fallback chain above, so which one a task runs at depends on whether its project
  declares a block. Reconciling them is its own decision.
- **A shipped threshold is not the same as a reachable one (#1144).** Satisfiability is
  measured at a turn budget manufactured to admit the threshold. Five shipped packs
  authored a `max_turns` too small for a run-side threshold of 10 to be reached inside
  it, so for those the surviving heuristic is live in principle and unreachable in
  practice — a budget question, not a heuristic one, and it is why the lock's claim is
  scoped to the threshold rather than to the pack.

## Links

- Related ADRs: [ADR-0032](0032-agent-completion-is-structural.md) (the other
  termination path decided structurally rather than by reading agent output).
- Related code: `tolokaforge/core/stuck.py`, `tolokaforge/core/conductor.py`,
  `tolokaforge/core/models/task_config.py`, `tolokaforge/core/models/run_config.py`.
- External references: #709, #1141 (looping content), #1142 (the reasoning-only stall),
  #1144 (threshold vs. authored turn budget), #1145 (the run-side mirror's 5-vs-10
  split), #677 (the activity lower bound `transcript_rules` shipped).
