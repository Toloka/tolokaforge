# 0046. Agentic LLM-as-Judge and the `JudgeKind` Registry

- **Status:** Accepted
- **Date:** 2026-09-10
- **Accepted-on:** 2026-09-10
- **Deciders:** @CiroGamboa
- **Consulted:** UK AISI Inspect AI (custom scorers), Meta Agent-as-a-Judge, RAGAS judge-alignment workflow, Harbor / Terminal-Bench llm-as-a-judge, "Catching One in Five" (2026) multi-turn judge evidence
- **Milestone:** 49
- **Supersedes:** none
- **Extends:** [0043 — Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract](0043-detached-mode-grader-and-typed-grader-kinds.md)

## Context

ADR-0043 shipped the typed `GraderKind` registry but left the LLM-judge
sub-component itself single-shot: one `LLMJudge.run` call produced one
`submit_report` verdict per trial, with no self-critique pass and no
plug-in seam above it. §Alternatives considered explicitly deferred a
model-graded kind — "the model-judge shape is a follow-up milestone; this
milestone shipped the seam so registration is a one-line entry-point" —
to a later milestone.

That single-shot judge had two capability caps. First, large rubrics
(30+ criteria) could overflow the judge model's output-token ceiling in
one `submit_report` payload, forcing a whole-trial `UNGRADEABLE` with no
recourse. Second, a judge given only one pass at the transcript had no
mechanism to reconsider its own draft verdicts against the evidence
before committing — the same single-turn-judge weakness the "Catching One
in Five" multi-turn evidence and Meta's Agent-as-a-Judge work both
motivate: a judge that can act like an agent (re-read evidence, critique
its own draft) catches errors a one-shot judge cannot.

Milestone 49 ("Judge integrity v2", umbrella #1562) closed both caps by
generalising the single-shot judge into a typed, pluggable seam — mirroring
ADR-0043's own pattern of turning a hardcoded branch into a registry — and
adding two new kinds behind it, one of which is agentic in the
`ToolCallingLoop` sense already established elsewhere in the engine.

## Decision

**Ship the `JudgeKind` Protocol and its entry-point registry.**
`tolokaforge.core.grading.judge_kinds._protocol.JudgeKind` is a
`runtime_checkable` `Protocol` with a `NAME: ClassVar[str]` (must equal
its entry-point name, so a `pyproject.toml` typo surfaces at discovery,
not at judge time) and a kwargs-only `evaluate(...) -> JudgeResult`
method mirroring `LLMJudge.run`'s per-trial evidence surface (`rubric`,
`agent_system_prompt`, `transcript`, `db_reader`, `kb_search`,
`workspace_dir`, `extra_read_tools`, `state_diff`) plus construction
inputs (`judge_model_config`, `judge_model_provider`), per-trial
customization (`disable_knowledge_search`, `custom_system_prompt`,
`include_agent_system_prompt`), and a `kind_config: Mapping[str, Any] |
None` handle each kind reads its own options from. Kinds register under
the `tolokaforge.judge_kinds` entry-point group, one line per kind — the
same registry shape ADR-0043 established for `tolokaforge.grader_kinds`.
A task selects its kind via `grading.llm_judge.judge_kind`; unknown kinds
are refused at parse time, never at judge time.

**Ship three built-in kinds.** `single_shot_rubric` (byte-identical with
the pre-seam `LLMJudgeRubricEvaluator`; the default) wraps `LLMJudge` in
one call. `chunked_rubric` splits the rubric's criteria into fixed-K
contiguous chunks and runs one `LLMJudge` invocation per chunk against a
scoped sub-rubric, closing the output-token-ceiling cap — any chunk that
errors or returns partial verdicts fails the whole trial loud, never a
silent partial verdict, and `JudgeResult.chunk_boundaries` records every
chunk attempted. `agentic_rubric` drives a draft → critique → submit
episode over a directly-constructed `ToolCallingLoop` (the same loop
machinery the coding-agent side of the engine already uses, not a new
engine): the judge calls `draft_report`, receives one engine-injected
critique prompt echoing its own draft verdicts back at it, may re-examine
evidence with its read tools and a real `critique(verdict_draft)` tool
(read-only, gated on `kind_config.enable_critique_tool`), then calls
`submit_report` for the final verdict. Both non-default kinds opt in via
`grading.llm_judge.judge_kind`; `single_shot_rubric` stays the default so
every existing task pack and run config grades identically.

**Ship a κ-based parity gate, not a byte-parity gate.** Because a judge
model's stochastic output can never be byte-identical to another kind's
output even when both are "correct", `tolokaforge.core.grading.judge_kinds.parity`
measures per-criterion Cohen's κ instead of byte equality, at three
levels (`ParityGateThresholds`, Landis-Koch anchored, aligned with the
MT-Bench / Langfuse ~80% judge-agreement bar): a candidate kind must
agree with the reference kind at `block: κ ≥ 0.8` per criterion
(cross-kind); a kind must agree with itself across replays at the
stricter `self_consistency_block: κ ≥ 0.7` (self-consistency); and `warn:
κ < 0.6` surfaces criteria that pass the block bar but are close enough
that future drift could push them under. Both new kinds pass this gate
against `single_shot_rubric` on the 20-fixture canonical parity corpus
before being default-eligible for a task pack.

**Ship bundle recording of the kind and its config.** `Grade` and the
grade bundle both record `judge_kind` + `kind_config` (and
`judge_chunk_boundaries` for chunking kinds) so an offline regrade
(`tolokaforge grade`, the CLI ADR-0043 shipped) reproduces the exact same
kind dispatch a trial graded under live, without re-deriving it from task
config that may have since changed.

## What ships and what stays follow-up

**Ships this milestone (#1562, Phases A–C):**

- `JudgeKind` Protocol + `tolokaforge.judge_kinds` entry-point registry + `single_shot_rubric` (#1566).
- `grading.llm_judge.judge_kind` config field + `kind_config` plumbing, refused at parse time when unknown (#1567).
- κ-based parity harness (`measure_cross_kind_agreement`, `measure_self_consistency`, `decide_parity_gate`) + canonical parity lane over the 20-fixture corpus (#1568).
- `chunked_rubric` `JudgeKind` (#1524).
- Bundle recording of `judge_kind` + `kind_config` + `chunk_boundaries`; offline replay dispatches through the `JudgeKind` seam (#1569).
- `agentic_rubric` `JudgeKind` — draft → critique → submit over `ToolCallingLoop` (#1570).
- The `critique(verdict_draft)` read-only tool, gated on `kind_config.enable_critique_tool` (#1571).
- This ADR, `docs/JUDGE_KINDS.md`'s Protocol contract + authoring guide + worked examples, and the `tools/judge-kind-ab` live cross-kind κ/cost framework (#1572).

**Stays follow-up:**

- A sub-agent or external-verifier judge kind (a kind that itself dispatches to another model or tool-using agent to adjudicate the primary judge's verdict) — cited as future work, not scoped to this milestone.
- `JudgeKind.evaluate`'s `explicit_system_prompt` widening for the bundle-branch `prompts.yaml.judge_prompt` escape hatch — follow-up #1583.
- Retiring the offline-replay `LLMJudge`-direct escape hatch once every consumer routes through a named kind.
- A human-labelled calibration corpus and a multi-judge jury kind — explicitly out of scope for this milestone (non-goals of #1572's own plan).

## Consequences

**Positive**

- Judge dispatch is a plug-in seam — a fourth kind (jury, sub-agent
  verifier, or a downstream package's own kind) adds one entry-point line
  and one class implementing `JudgeKind`, no framework PR.
- Per-criterion κ gives per-criterion accountability: a new kind's
  agreement (or disagreement) with the reference is visible criterion by
  criterion, not collapsed into one pass/fail trial-level signal.
- `agentic_rubric` reuses the engine's existing `ToolCallingLoop`
  machinery instead of introducing a second agent-loop implementation —
  the same invariant ADR-0043 leaned on ("grading is a pure function of a
  substrate") extends cleanly to "a judge kind is a pure function of the
  evidence surface", regardless of how many internal turns it takes.
- Bundle-recorded `judge_kind` + `kind_config` makes offline regrade
  (`tolokaforge grade`) deterministic across the kind axis, not just the
  substrate axis ADR-0043 already covered.

**Negative**

- The κ-parity corpus (20 fixtures today) is a maintenance surface: every
  new kind, and every prompt change to an existing kind, needs cassette
  fixtures re-authored or re-recorded (`--live-parity`, wired by #1572)
  before the gate can run.
- `agentic_rubric`'s draft → critique → submit episode costs strictly
  more latency and LLM spend per trial than `single_shot_rubric` — three
  model calls minimum instead of one, widened turn/timeout budgets
  (50 turns / 480s vs. 14/240s) to cover the extra turns. Task packs
  should opt into it deliberately, not by default.
- `chunked_rubric` costs one `LLMJudge` invocation per chunk instead of
  one per trial, so its per-trial spend scales with rubric size divided
  by chunk size, not with a single flat call.

## Alternatives considered

- **Single hard-coded evaluator forever.** Never generalise past the
  pre-seam single-shot judge. Reject: caps every future judge-side
  direction (chunking, self-critique, jury, sub-agent verification) behind
  a framework PR each time — exactly the shape ADR-0043 rejected for
  grader kinds, and the umbrella's own framing for this milestone.
- **Byte-parity gate for all kinds.** Require a candidate kind's output to
  be byte-identical to the reference kind's, the same bar ADR-0043's
  10-pack byte-parity lanes use for substrate topology. Reject: judges are
  stochastic past the reference wrap (deterministic in the reference
  `single_shot_rubric` cassette lane, but not for a live LLM call, and
  never for a kind whose internal reasoning path genuinely differs from
  the reference, such as `agentic_rubric`'s multi-turn critique).
  Byte-parity is the right bar for grading *substrates* re-reading the
  same recorded state; it is the wrong bar for judge *models* forming
  independent judgements. A κ-agreement bar measures "do these two
  independently-formed verdicts agree", which is the actual question.
- **One monolithic agentic judge, no kind registry.** Ship only the
  agentic draft → critique → submit judge and retire single-shot
  entirely. Reject: single-shot's determinism and low cost remain the
  right default for the common case (small rubrics, no need for
  self-critique); forcing every task pack onto the agentic kind's extra
  latency and spend with no opt-out would regress every existing task
  pack's grading cost for no benefit on rubrics that do not need it.
- **Sub-agent / external-verifier judge kind in this milestone.** Extend
  the built-ins with a fourth kind that itself calls out to another model
  or tool-using agent to adjudicate. Reject: this milestone's scope
  (#1562) is the seam plus the chunking and self-critique kinds; a
  verifier-of-a-verifier kind is real future work but was not needed to
  close either of ADR-0043's deferred caps, and adding it now would widen
  this milestone's already-large surface (Phases A through C) rather than
  close it.

## Follow-ups

Open at ADR authorship (2026-09-10):

- **#1583** — widen `JudgeKind.evaluate`'s signature to accept `explicit_system_prompt`, letting the bundle-branch `prompts.yaml.judge_prompt` escape hatch route through the seam instead of constructing `LLMJudge` directly.

## Cross-references

- Extends: [ADR-0043 — Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract](0043-detached-mode-grader-and-typed-grader-kinds.md).
- `JudgeKind` Protocol, authoring guide, worked examples, and the parity gate: [`docs/JUDGE_KINDS.md`](../JUDGE_KINDS.md).
- Grade bundle recording of `judge_kind` / `kind_config`: [`docs/GRADE_BUNDLE.md`](../GRADE_BUNDLE.md).
- Grading dispatch: [`docs/GRADING.md`](../GRADING.md).
- Milestone: 49 ("Judge integrity v2", umbrella #1562).
