# 0046. The `JudgeKind` Protocol and Entry-Point Registry

- **Status:** Accepted
- **Date:** 2026-09-10
- **Accepted-on:** 2026-09-10
- **Deciders:** @CiroGamboa
- **Consulted:** UK AISI Inspect AI (custom scorers), Harbor / Terminal-Bench llm-as-a-judge
- **Milestone:** 49
- **Supersedes:** none
- **Extends:** [0043 — Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract](0043-detached-mode-grader-and-typed-grader-kinds.md)

## Context

ADR-0043 shipped the typed `GraderKind` registry but left the LLM-judge
sub-component itself single-shot: one `LLMJudge.run` call produced one
`submit_report` verdict per trial, with no plug-in seam above it. §Alternatives
considered explicitly deferred a model-graded kind — "the model-judge shape is
a follow-up milestone; this milestone shipped the seam so registration is a
one-line entry-point" — to a later milestone.

That single-shot judge had one capability cap: large rubrics (30+ criteria)
could overflow the judge model's output-token ceiling in one `submit_report`
payload, forcing a whole-trial `UNGRADEABLE` with no recourse.

Milestone 49 ("Judge integrity v2", umbrella #1562) closes that cap by
generalising the single-shot judge into a typed, pluggable seam — mirroring
ADR-0043's own pattern of turning a hardcoded branch into a registry — and
adding a chunking kind behind it. This is Phase A of a larger Grader v3 seam:
the Protocol and registry are designed so a downstream package can register
its own kind (jury, sub-agent verifier, or otherwise) without a framework PR.

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

**Ship two built-in kinds.** `single_shot_rubric` (byte-identical with
the pre-seam `LLMJudgeRubricEvaluator`; the default) wraps `LLMJudge` in
one call. `chunked_rubric` splits the rubric's criteria into fixed-K
contiguous chunks and runs one `LLMJudge` invocation per chunk against a
scoped sub-rubric, closing the output-token-ceiling cap — any chunk that
errors or returns partial verdicts fails the whole trial loud, never a
silent partial verdict, and `JudgeResult.chunk_boundaries` records every
chunk attempted. `chunked_rubric` opts in via `grading.llm_judge.judge_kind`;
`single_shot_rubric` stays the default so every existing task pack and run
config grades identically.

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
that future drift could push them under. `chunked_rubric` passes this
gate against `single_shot_rubric` on the 20-fixture canonical parity
corpus before being default-eligible for a task pack.

**Ship bundle recording of the kind and its config.** `Grade` and the
grade bundle both record `judge_kind` + `kind_config` (and
`judge_chunk_boundaries` for chunking kinds) so an offline regrade
(`tolokaforge grade`, the CLI ADR-0043 shipped) reproduces the exact same
kind dispatch a trial graded under live, without re-deriving it from task
config that may have since changed.

## What ships and what stays follow-up

**Ships this milestone (#1562, Phase A):**

- `JudgeKind` Protocol + `tolokaforge.judge_kinds` entry-point registry + `single_shot_rubric` (#1566).
- `grading.llm_judge.judge_kind` config field + `kind_config` plumbing, refused at parse time when unknown (#1567).
- κ-based parity harness (`measure_cross_kind_agreement`, `measure_self_consistency`, `decide_parity_gate`) + canonical parity lane over the 20-fixture corpus (#1568).
- `chunked_rubric` `JudgeKind` (#1524).
- Bundle recording of `judge_kind` + `kind_config` + `chunk_boundaries`; offline replay dispatches through the `JudgeKind` seam (#1569).
- This ADR, `docs/JUDGE_KINDS.md`'s Protocol contract + authoring guide + worked examples, and the `tools/judge-kind-ab` live cross-kind κ/cost framework (#1572).

**Stays follow-up:**

- A self-critique, jury, or sub-agent / external-verifier judge kind — the
  Protocol and registry are shaped to accept one without a framework PR, but
  none ships in this milestone.
- `JudgeKind.evaluate`'s `explicit_system_prompt` widening for the bundle-branch `prompts.yaml.judge_prompt` escape hatch — follow-up #1583.
- Retiring the offline-replay `LLMJudge`-direct escape hatch once every consumer routes through a named kind.
- A human-labelled calibration corpus — explicitly out of scope for this milestone.

## Consequences

**Positive**

- Judge dispatch is a plug-in seam — a new kind (self-critique, jury,
  sub-agent verifier, or a downstream package's own kind) adds one
  entry-point line and one class implementing `JudgeKind`, no framework PR.
- Per-criterion κ gives per-criterion accountability: a new kind's
  agreement (or disagreement) with the reference is visible criterion by
  criterion, not collapsed into one pass/fail trial-level signal.
- Bundle-recorded `judge_kind` + `kind_config` makes offline regrade
  (`tolokaforge grade`) deterministic across the kind axis, not just the
  substrate axis ADR-0043 already covered.

**Negative**

- The κ-parity corpus (20 fixtures today) is a maintenance surface: every
  new kind, and every prompt change to an existing kind, needs cassette
  fixtures re-authored or re-recorded (`--live-parity`, wired by #1572)
  before the gate can run.
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
  the reference). Byte-parity is the right bar for grading *substrates*
  re-reading the same recorded state; it is the wrong bar for judge
  *models* forming independent judgements. A κ-agreement bar measures "do
  these two independently-formed verdicts agree", which is the actual
  question.
- **Sub-agent / external-verifier judge kind in this milestone.** Extend
  the built-ins with a kind that itself calls out to another model or
  tool-using agent to adjudicate. Reject: this milestone's scope (#1562)
  is the seam plus the chunking kind; a verifier-of-a-verifier kind is
  real future work but was not needed to close ADR-0043's deferred cap,
  and adding it now would widen this milestone's surface rather than
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
