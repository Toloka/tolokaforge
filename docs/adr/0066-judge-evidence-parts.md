# 0066. Judge evidence made of named parts

- **Status:** Accepted
- **Date:** 2026-11-03
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

The judge already had three independent switches over what it is shown — the
knowledge-search tools, the agent's policy, the snippet length — each a key of its
own under `customization`. A transcript-only judge is a fourth combination of the
same kind of switch, not a different judge.

## Decision Drivers

- Opt-in per task; a pack that sets nothing is graded byte-for-byte as before.
- One vocabulary for every view of the trial the judge reads, so a new view is one
  more entry, not one more mode.
- No view of the final state reaches a judge whose parts exclude it, by construction.
- Replayable from the bundle like every other judge setting, and overridable at replay.
- No change to the `JudgeKind` Protocol: every kind gets the evidence through its options.
- Uses the tool author's `mutates_state` (ADR-0061) where it exists.

## Considered Options

1. **A mode switch, `evidence: state | transcript`.** It forks `LLMJudge` into two
   opening-message builders, guards the mode at every call site, records it beside
   the customization in four places, and keeps a judge-only list of which calls read.
2. **A new judge kind.** Every wrapping kind (`voted`, `auto`, `multi_turn`) would
   need a transcript twin, and the view would be bound to one kind.
3. **Keep it outside the engine.** No replay, no reuse, and each pack's variant drifts.
4. **Named parts.** `customization.evidence` is a set of independent parts, each
   contributing its section of the opening message, its tools and the trial views it
   needs.

## Decision

We will adopt **Option 4**.

- `customization.evidence` has seven parts: `agent_policy`, `knowledge_search`
  (`{snippet_chars}`), `state_diff`, `db`, `workspace`, `tool_results` (`{max_chars}`)
  and `state_changing_calls`. Each is `false` (off), `true` (on with default options)
  or a mapping (on with those options); there is no preset. The defaults are the judge
  over the final state, byte for byte, and are left off the wire. The keys the parts
  replace — `disable_knowledge_search`, `include_agent_system_prompt`,
  `judge_snippet_chars` — are refused wherever a customization is read, naming the
  replacement.
- Each part is an `EvidencePart` (`core/grading/judge_evidence.py`): its section of
  the opening message, its closing instruction, the tools it offers, the recorded
  tools replay shims for it, and whether it reads the numbered transcript. One
  composition builds the opening message and the tool surface from the parts that are
  on; the opening message names only tools that are offered. An extra read tool a
  caller hands the judge names the part that owns it, and is offered only while that
  part is on.
- `JudgeEvidence.needs` (`runner/models.py`) is the one mapping from parts to the
  trial views a caller builds — the state diff, the DB reader, the workspace, the
  tools' declarations. The runner, the grader service, the `judge_only` grader and
  replay build each view only when it is needed, and `LLMJudge` refuses one handed
  without its part.
- `tool_results` numbers every tool result `[call N]`, cuts it at `max_chars` and
  offers `show_call(calls, offset)`, which returns full results from the same
  transcript; it is a pure function of the transcript, so replay rebuilds it and never
  shims it. `state_changing_calls` lists the agent's calls that can change state, each
  with the first line of the result the agent received; on without `tool_results` the
  results are numbered and not cut.
- Whether a call can change state belongs to the tool's declaration, ADR-0061
  `mutates_state`, which gains a third form: `{read_when: {args: {<path>:
  <predicate>}}}` in the trace matcher's own vocabulary, evaluated through the
  matcher's resolver (`core/grading/argument_predicates.py`). It is declared by the
  tool's author — the MCP `_meta` key `tolokaforge/mutates_state`, or a builtin's
  `ToolPolicy` (`http_request` declares its safe methods, `GET`, `HEAD`, `OPTIONS`,
  as reads) — or by the task on `tools.<actor>.<name>.mutates_state` where the author
  is silent, or as `true` to tighten. An undeclared tool reads as one that can change
  state.
- How a call ended is read from its result's body, the channel
  `core/tool_message_format.py` declares: a call the harness saw fail carries the
  agent-facing `Error: ` prefix. The grade-time transcript wire carries no status, so
  the block is the same on every path, the standalone grader service included.
- The effective customization a trial was graded under — after the project, task and
  run-level `grader.judge` layers, part by part — is recorded once, as
  `judge_inputs.yaml customization` (`JudgeReport.customization_json`). Replay layers
  a `rejudge --grading` customization over that record, so `--grading` is the one
  override of every part; a part it turns on whose view the bundle never recorded is
  refused.

## Consequences

### Positive

- A pack switches the judge to the transcript alone, or keeps any subset of the
  views, with keys of one vocabulary, and can A/B it over a recorded run with a
  customization-only `tolokaforge rejudge --grading` document.
- One composition, one part-to-view mapping and one record of the customization:
  adding a view is adding a part.
- The first runtime consumer of ADR-0061's `mutates_state`.
- Every judge kind gains the parts without a change.

### Negative / Trade-offs

- Three shipped task-pack keys are renamed into `evidence.*`; a pack carrying one is
  refused at load until migrated, and so is a bundle recorded with one at replay.
- A judge without the final-state parts cannot verify a claim the transcript does not
  settle; such a criterion belongs in deterministic checks.
- An undeclared tool's reads are listed as state-changing until the tool or its task
  declares `mutates_state`.
- Each tool's declaration is recorded on its `tools_schemas.yaml` entry (bundle
  generation 8); a bundle predating it replays every part except
  `state_changing_calls`.
- The block shows a result's first line, not a typed status: a tool that states its
  outcome further down its result, or an MCP error reply (`isError: true`), whose text
  reaches the agent without the `Error: ` prefix, reads only as the agent saw it.

## Links

- Related ADRs: [0046](0046-judgekind-protocol-and-registry.md), [0061](0061-mutates-state-on-the-tool-wire.md).
- Related code: `core/grading/judge_evidence.py`, `core/grading/judge.py`
  (`LLMJudge`), `core/grading/judge_kinds/options.py`, `core/tool_effects.py`,
  `core/grading/argument_predicates.py`, `core/grading/composite/llm_judge.py`,
  `core/grading/replay.py`, `runner/models.py` (`JudgeCustomization`,
  `JudgeEvidence`, `MutatesStateRule`).
- Documentation: `GRADING.md` § Judge evidence, `CONFIG.md`, `JUDGE_REPLAY.md`,
  `OUTPUT_FORMAT.md`, `PROJECTS.md`, `JUDGE_KINDS.md`.
