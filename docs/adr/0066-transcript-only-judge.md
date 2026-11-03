# 0066. A transcript-only judge: grade from what the agent said and did, never from the final state

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The rubric judge grades over the final state: it reads the `initial → final` state
diff and can call `get_db_state`, `query_db` and `read_file`. Many packs already
check the final state deterministically (`custom_checks`, `state_checks`) and use
the judge only for what the agent communicated: the replies it gave and the texts
it wrote through its tools. For them the state view is redundant, and expensive:
packs measured a judge with state access at several times the cost of one without,
with no visible difference in verdict quality.

Removing the state view leaves two gaps. The judge no longer sees what the agent
changed, only what it called; and a long tool result, which the state diff used to
summarise, now fills the judge's context. Packs worked around both outside the
engine, each in its own way, so the variant could not be replayed, compared or
reused.

## Decision Drivers

- Opt-in per task; a pack that sets nothing is graded byte-for-byte as before.
- No state reaches a transcript judge, by construction, not by convention.
- Replayable from the bundle like every other judge setting.
- No change to the `JudgeKind` Protocol: every kind gets the mode through its options.
- Uses the tool author's `mutates_state` (ADR-0061) where it exists.

## Considered Options

1. **A customization key, `evidence: state | transcript`, carried in
   `JudgeTrialOptions`.** `LLMJudge` builds the transcript view itself.
2. **A new judge kind.** Every wrapping kind (`voted`, `auto`, `multi_turn`)
   would need a transcript twin, and the mode would be bound to one kind.
3. **Keep it outside the engine.** No replay, no reuse, and each pack's variant drifts.

## Decision

We will adopt **Option 1**.

- `grading.llm_judge.customization` gains `evidence` (`state` by default) and
  `transcript_tool_chars` (`2000`), inert under `state`. Both are left off the wire at
  their default, so an older image accepts every pack that does not set them.
- Under `transcript` `build_judge_state_diff` builds no diff and the composite hands
  the judge kind no DB reader and no workspace; `LLMJudge` refuses any of the three if
  a caller passes one, and the composite refuses a kind that completes such a trial
  without honouring the mode. Knowledge-search tools are unaffected.
- The judge reads the transcript with every tool result numbered `[call N]` and cut at
  `transcript_tool_chars`; a block of the agent's state-changing calls, each with how
  it ended (a failed call with the first line of its result, where an HTTP tool states
  its code) and its arguments cut short — the transcript shows them in full; and
  `show_call(calls, offset)`, which returns full results from the same transcript. A
  call is joined to its result by the episode-unique key of its id, as the trace
  timeline joins them. `show_call` is a pure function of the transcript, so it is never shimmed
  in replay and is not in `read_tools_offered`.
- A call can change state unless its tool's ADR-0061 `mutates_state` says it reads:
  `false`, or a `{read_when: {args: {<path>: <predicate>}}}` rule in the trace
  matcher's own vocabulary, evaluated through the matcher's resolver
  (`core/grading/argument_predicates.py`), which is how a tool that both reads and
  writes (an HTTP tool) marks its reads. A task declares the rule on
  `tools.<actor>.<name>.mutates_state` where the tool's author is silent, or `true` to
  tighten. An undeclared tool reads as one that can change state, as ADR-0061
  prescribes.
- How a call ended (`ToolExecutionStatus`) is added to the grade-time transcript wire
  as `tool_status` when recorded. Only the transcript judge reads it, from the payload:
  `decode_transcript_wire` leaves it off the decoded messages, because a trace timeline
  built without tool-call records reads a call's status from the records alone, and a
  status there would decide a `status:` matcher that is undecided today.
- Each tool's declaration is recorded on its `tools_schemas.yaml` entry (bundle
  generation 8), whatever the judge graded from, so a state judge's trial replays as a
  transcript judge too; replay reads the file only for a transcript judge.
  `JudgeResult.evidence`,
  `JudgeReport.judge_evidence` (field 19, set only for a transcript judge) and
  `grade.yaml` `judge_evidence` say which mode graded the trial. Replay rebuilds the
  whole view from the transcript, the recorded customization and the recorded
  declarations, stamps `judge_evidence` in its provenance, and
  `rejudge --evidence state|transcript` forces the mode over a whole run.

## Consequences

### Positive

- A pack switches the judge to the cheaper mode with one key, and can A/B it over a
  recorded run with `tolokaforge rejudge --evidence transcript`.
- The first runtime consumer of ADR-0061's `mutates_state`.
- Every judge kind gains the mode without a change.

### Negative / Trade-offs

- A transcript judge cannot verify a claim the transcript does not settle; such a
  criterion belongs in deterministic checks.
- An undeclared tool's reads are listed as state-changing until the tool or its task
  declares `mutates_state`.
- `TRIAL_CONTRACT_VERSION` moves to 2: `TrialNodeInputs` and `TrialResult` gain
  optional fields.
- The block states statuses in the engine's vocabulary, not a protocol's: an HTTP
  code reaches the judge only through the first line of a failed call's result.

## Links

- Related ADRs: [0046](0046-judgekind-protocol-and-registry.md), [0061](0061-mutates-state-on-the-tool-wire.md).
- Related code: `core/grading/judge_transcript.py`, `core/grading/judge.py`
  (`LLMJudge`), `core/grading/judge_kinds/options.py`, `core/tool_effects.py`,
  `core/grading/argument_predicates.py`, `core/grading/composite/llm_judge.py`,
  `core/grading/transcript_wire.py`, `core/grading/replay.py`, `runner/models.py`
  (`JudgeCustomization`, `MutatesStateRule`).
- Documentation: `GRADING.md` § Transcript-only judge, `CONFIG.md`, `JUDGE_REPLAY.md`,
  `OUTPUT_FORMAT.md`, `PROJECTS.md`, `JUDGE_KINDS.md`.
