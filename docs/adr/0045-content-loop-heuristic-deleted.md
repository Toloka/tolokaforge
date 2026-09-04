# 0045. StuckDetector runs one heuristic — repeated identical result

- **Status:** Accepted
- **Date:** 2026-09-04
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Extends:** [ADR-0035](0035-idle-turn-heuristic-deleted.md)

## Context and Problem Statement

`StuckDetector` shipped with two heuristics. The first, `_has_repeated_tool_calls`,
now reads the outcome: identity is `(tool_name, arguments, sha256(output))`,
so the classifier fires only when the same tool call produced byte-identical
result bytes across the window. The second, `_has_looping_content`, was a
3-word trigram counter over the last 10 assistant messages, firing when the
most-common trigram reached count ≥ 10. ADR-0035 § "Still unaddressed"
recorded that this heuristic sees only *intra-message* repetition — twelve
copies of the same phrase inside one assistant message — and never
inter-turn repetition, because the trigram counter aggregates trigrams from
all messages in the window rather than counting messages that share a
trigram, so a phrase repeated across turns contributes at most one trigram
occurrence per turn and cannot reach the threshold in a ten-message window.

Once the surviving heuristic reads the *outcome*, "same work, no progress"
is answered where the evidence is: the recorded tool call and its recorded
result bytes. A phrase repeated inside one message is a different question
— it is degeneracy in the sampler, and terminating on it removes the
evidence a grader would need to explain the trial.

## Decision Drivers

- **The class contract is one question.** `is_stuck` is meant to answer
  "same work, no progress". After the result-aware retune, `_has_repeated_tool_calls`
  answers it at the observable surface: the tool call and what came back.
  A second heuristic that answers a different question (intra-message
  phrase repetition) reads as a second answer to the same question and
  drifts the classifier's meaning.
- **The remaining heuristic never fired inter-turn.** ADR-0035 § "Still
  unaddressed" measured this against real dialogues and referenced #1141.
  A control that reads as live in the config, the docs and the detector
  but only fires on a synthetic 12×-inside-one-message pattern answers no
  question a real trial would ask.
- **Sampler degeneracy is not a stall.** A model that repeats a phrase
  twelve times inside one message is producing degenerate output — a
  grading-side observation, not a run-termination signal. Ending the trial
  on it discards the transcript state the grader would have read.
- **`is_stuck` no longer reads messages.** The narrower
  `is_stuck(tool_calls)` matches what the classifier actually needs, and
  a phantom `messages` parameter reads as "content policy might come back".

## Considered Options

1. **Repair `_has_looping_content` to count message-level repetition** —
   the trigram counter converted to a per-message set. Rejected: the
   question this would answer is already answered by
   `_has_repeated_tool_calls` at the outcome-level for tool-issuing agents,
   and a prose-only stall is a per-task assertion (`transcript_rules` in
   `grading.yaml`), not a harness-global rule (ADR-0035 § Decision).
2. **Leave both heuristics live.** Rejected: two heuristics with different
   semantics answering the same class contract creates a second answer to
   the same question; the class becomes what one heuristic fires on OR
   what the other fires on, which is not a coherent classifier.
3. **Delete `_has_looping_content`, narrow `is_stuck` to
   `(tool_calls)`, close #1141.** **This ADR.**

## Decision

We adopt **Option 3**. `_has_looping_content`, the `MessageRole` /
`Message` imports it required, and the `messages` parameter on `is_stuck`
are deleted. The single production caller
(`TrialRunner._agent_termination`) is narrowed in the same commit.

The class contract collapses to one line: fire when the last
``max_repeated_tool_calls`` recorded tool calls all carry the same
``(tool_name, arguments, sha256(output))`` identity. Whether the *task*
additionally requires the agent to act is a per-task assertion, answered
by ``transcript_rules`` in the task's ``grading.yaml`` (ADR-0035 § Decision).

`tests/canonical/test_stuck_heuristics_are_satisfiable.py` continues to
discover the predicate set from `vars(StuckDetector)`; after this change
the discovered set is `{"_has_repeated_tool_calls"}`, the driving-case
map matches, and the varied-prose lock (`_script_varied_prose` runs to
`max_turns` at every shipped threshold) hardens now that only tool-call
identity can fire the detector.

## Consequences

### Positive

- The classifier answers one question, and its answer is grounded in the
  evidence a grader can also read (the recorded tool call and its recorded
  result bytes).
- The `messages` parameter no longer reads as future content policy.
- The trigrams-inside-one-message shape no longer terminates a trial
  before the grader sees the state that caused the sampler degeneracy.

### Negative / Trade-offs

- **A trial that would have terminated `stuck_detected` via
  `_has_looping_content` now runs to whichever terminator applies next**
  (typically `max_turns`), then is graded rather than auto-failed. Per
  ADR-0035 § Consequences the shape this heuristic fires on is a phrase
  repeated twelve times inside a single assistant message — a rare,
  largely synthetic pattern.
- `stuck_rate` values on recorded runs are byte-unchanged: the removal
  only affects future runs.

### Still unaddressed

- **The reasoning-only stall (#1142).** Same open item ADR-0035 named —
  typed evidence (`finish_reason`, reasoning-only output), not a turn
  count, and orthogonal to the tool-call classifier this ADR keeps.
- **`max_repeated_tool_calls` still has two live answers (#1145)**: 5 on
  the task-scope `StuckHeuristicsDefaults` and 10 on the run-side
  `StuckHeuristics`. Reconciling them is its own decision.
- **A shipped threshold is not the same as a reachable one (#1144).**
  Satisfiability is measured at a turn budget manufactured to admit the
  threshold; whether a pack's authored `max_turns` leaves room for the
  surviving heuristic is a budget question.

## Links

- Extends [ADR-0035](0035-idle-turn-heuristic-deleted.md) — resolves its
  § "Still unaddressed" entry for #1141 by deletion.
- Related code: `tolokaforge/core/stuck.py`, `tolokaforge/core/runner.py`,
  `tests/canonical/test_stuck_heuristics_are_satisfiable.py`.
- External references: #1141 (closes — `_has_looping_content` sees only
  intra-message repetition), #1142 (reasoning-only stall, still open),
  #1144 (threshold vs. authored turn budget, still open), #1145 (task-scope
  vs. run-side default reconciliation, still open).
