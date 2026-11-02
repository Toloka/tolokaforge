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

- `grading.llm_judge.customization` gains `evidence` (`state` by default),
  `transcript_tool_chars` (`2000`) and `read_only_calls` (a list of
  `{tool, arguments}` rules). The last two apply only to `transcript` and are refused
  under `state` away from their defaults. All three are left off the wire at their
  default, so an older image accepts every pack that does not set them.
- Under `transcript` the composite builds no state diff and hands the judge kind no
  DB reader and no workspace; `LLMJudge` refuses any of the three if a caller passes
  one. Knowledge-search tools are unaffected.
- The judge reads the transcript with every tool result numbered `[call N]` and cut at
  `transcript_tool_chars`; a block of the agent's state-changing calls, each with how
  it ended; and `show_call(calls, offset)`, which returns full results from the same
  transcript. `show_call` is a pure function of the transcript, so it is never shimmed
  in replay and is not in `read_tools_offered`.
- A call can change state unless its tool declares `mutates_state: false` or a
  `read_only_calls` rule matches it. A rule names a tool and, optionally, dotted
  argument paths with the values that make a call a read, which is how a task marks
  the reads of a tool that both reads and writes (an HTTP tool). An undeclared tool
  reads as one that can change state, as ADR-0061 prescribes.
- How a call ended (`ToolExecutionStatus`) is added to the grade-time transcript wire
  as `tool_status`, only when recorded, so a payload without statuses is unchanged.
- The agent tools the judge read as read-only are recorded: `JudgeResult` and the
  `JudgeReport` proto (field 18 on both wires) carry `agent_read_only_tools`, and
  `judge_inputs.yaml` stores it, because the bundle does not carry the tool schemas.
  Replay rebuilds the whole view from the transcript, the recorded customization and
  this list, and stamps `judge_evidence` in its provenance.

## Consequences

### Positive

- A pack switches the judge to the cheaper mode with one key, and can A/B it through
  `tolokaforge rejudge --grading`.
- The first runtime consumer of ADR-0061's `mutates_state`.
- Every judge kind gains the mode without a change.

### Negative / Trade-offs

- A transcript judge cannot verify a claim the transcript does not settle; such a
  criterion belongs in deterministic checks.
- An undeclared tool's reads are listed as state-changing until the task adds a rule
  or the tool declares `mutates_state`.
- `TRIAL_CONTRACT_VERSION` moves to 2: `TrialNodeInputs` and `TrialResult` gain
  optional fields.

## Links

- Related ADRs: [0046](0046-judgekind-protocol-and-registry.md), [0061](0061-mutates-state-on-the-tool-wire.md).
- Related code: `core/grading/judge_transcript.py`, `core/grading/judge.py`
  (`LLMJudge`), `core/grading/judge_kinds/options.py`,
  `core/grading/composite/llm_judge.py`, `core/grading/transcript_wire.py`,
  `core/grading/replay.py`, `runner/models.py` (`JudgeCustomization`, `ReadOnlyCallRule`).
- Documentation: `GRADING.md` § Transcript-only judge, `CONFIG.md`, `JUDGE_REPLAY.md`,
  `OUTPUT_FORMAT.md`, `PROJECTS.md`, `JUDGE_KINDS.md`.
