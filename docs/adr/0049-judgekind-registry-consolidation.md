# 0049. JudgeKind registry consolidation — three shipping kinds

- **Status:** Accepted
- **Date:** 2026-09-23
- **Deciders:** @CiroGamboa
- **Milestone:** 51
- **Supersedes:** [0046 — The `JudgeKind` Protocol and Entry-Point Registry](0046-judgekind-protocol-and-registry.md)
- **Extends:** [0043 — Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract](0043-detached-mode-grader-and-typed-grader-kinds.md)

## Context

ADR-0046 (M49) shipped the `JudgeKind` Protocol, the
`tolokaforge.judge_kinds` entry-point registry, the κ-based parity gate, and
`single_shot_rubric` + `chunked_rubric` as the first two built-ins. M50
extended the registry with three further kinds — `voted_rubric`,
`jury_rubric`, `per_criterion_rubric` — plus `auto_anchored_rubric` as a
wrapper kind, and hung `Criterion.chunk_group` / `Grade.judge_chunk_boundaries`
off the wire to let `chunked_rubric` and its authoring surface plumb chunk
context through the bundle.

Six kinds landed. Empirical A/B evals then measured which of the drift
classes each kind was designed against actually appear at the bar the
parity gate uses (Landis-Koch anchored, κ ≥ 0.8 cross-kind, κ ≥ 0.7 self).

## Decision Drivers

- **Only two drift classes reliably move the κ needle on shipped rubrics.**
  Sampling noise (same judge, same input, different sample-time seed drift)
  → `voted_rubric` closes it by taking the majority verdict across K samples
  and refusing on any parseable-verdict shortfall. Wording ambiguity
  (rubric text a judge model reads two ways across sessions) →
  `auto_anchored_rubric` closes it by emitting one warm-up call that
  produces an in-session anchor summary of each criterion, then wrapping
  the chosen inner kind for the scoring call. A single `auto_anchored_rubric`
  wrapping `voted_rubric` therefore closes both classes with one recipe.
- **Chunk-context loss did not surface on shipped rubrics.** `chunked_rubric`
  was designed against the output-token-ceiling cap ADR-0046 named for
  rubrics of 30+ criteria and against inter-criterion context loss when
  a rubric is naturally partitioned. Neither showed up in A/B on the
  rubrics M49 and M50 measured — the rubrics that shipped either fit
  under the ceiling in one call or did not have a partition boundary
  the judge model degraded across.
- **Jury adds cost, not signal.** `jury_rubric` (independent judges of
  different provider families, verdict aggregation) improved κ within
  parity gate noise on the same measurements that established the two
  drift classes above; the additional per-trial spend (N provider calls
  vs. one wrap of the winning inner kind) did not clear a cost/benefit
  bar this milestone would defend.
- **Per-criterion isolates the wrong axis.** `per_criterion_rubric`
  (one `LLMJudge` call per criterion) reduces cross-criterion prompt
  bleed, which was not a measured drift class on shipped rubrics, and
  costs one call per criterion instead of one per trial.
- **The registry stays plug-in.** Everything ADR-0046 promised about
  adding a kind via one entry-point line is unchanged. A downstream
  package that measures a different drift class on its own rubrics
  registers its own kind — the framework does not need to carry a kind
  it cannot show as load-bearing.

## Decision

**The registry ships three kinds.** `single_shot_rubric` (default,
byte-identical with the pre-seam `LLMJudgeRubricEvaluator`),
`voted_rubric` (K samples of an inner kind, majority verdict per
criterion, refuses on parseable-verdict shortfall), and
`auto_anchored_rubric` (warm-up call producing per-criterion anchor
summaries, then wraps a chosen inner kind for scoring). The composed
recipe — `auto_anchored_rubric` wrapping `voted_rubric` — is the one
form that closes both measured drift classes on the shipped rubrics.

**`chunked_rubric`, `jury_rubric`, and `per_criterion_rubric` are
deleted from the registry.** So are the wire fields that only they
consumed: `Criterion.chunk_group`, `Grade.judge_chunk_boundaries`, the
`chunk_boundaries_wire` codec, and the `judge_scripts_per_chunk` parity
mechanism. Proto tag 16 (`chunk_boundaries_json`) on `JudgeReport` is
reserved in both `grader.proto` and `runner.proto` so a future field
cannot reuse it and silently decode a bundle recorded before this pass.

**Parity coverage is reduced to the shipping kinds.** The κ-based
parity gate ADR-0046 shipped continues to run on the 20-fixture
canonical corpus; the deleted kinds' parity assertions are removed and
`voted_rubric` + `auto_anchored_rubric` self- and cross-kind assertions
against `single_shot_rubric` take their place.

## What Ships

- Registry surface: `single_shot_rubric`, `voted_rubric`,
  `auto_anchored_rubric`. `grading.llm_judge.judge_kind` continues to be
  the config field; unknown kinds are refused at parse time.
- Wire: proto tag 16 reserved on both `JudgeReport` messages;
  `Criterion.chunk_group` and `Grade.judge_chunk_boundaries` removed.
- Parity: self- and cross-kind κ assertions for the three shipping
  kinds, on the same 20-fixture corpus.
- Docs: this ADR; ADR-0046 flipped to `Superseded by ADR-0049`;
  `docs/JUDGE_KINDS.md`, `docs/GRADE_BUNDLE.md`, `docs/GRADING.md`,
  `docs/JUDGE_REPLAY.md`, and the task-description schema updated to
  the three-kind surface.

## Consequences

### Positive

- One clear recipe closes both measured drift classes:
  `auto_anchored_rubric` wrapping `voted_rubric`. A task author picking
  a kind reads three lines, not six, and one of them names the
  drift-class question the choice answers.
- Reduced surface — three kinds, one wire path per kind, one parity
  assertion per kind — cuts the maintenance cost ADR-0046 § Consequences
  named for the parity corpus (every kind × every prompt-change needs
  cassette re-recording).
- Proto tag 16 reserved on both messages means a future field cannot
  reuse the slot and silently decode a bundle recorded before this
  pass as if the new field were the old one.

### Negative

- **Downstream packages that wanted to compose a chunked or jury variant
  now register their own kind.** The registry seam is unchanged — one
  entry-point line — but the shipping surface no longer carries the
  code they would have subclassed. Packages that were already
  registering their own kind are unaffected.
- **The output-token-ceiling cap ADR-0046 named is not addressed by
  this registry.** A rubric that overflows one `submit_report` payload
  today still fails loud (no silent partial verdict) but has no
  in-registry recipe to split. If a shipped rubric hits the cap, the
  path forward is either splitting the rubric at authoring time or
  registering a chunking kind out-of-tree.

## Alternatives Considered

- **Keep all six kinds.** Rejected: three of them measured within the
  parity gate's noise band on the drift classes they were designed
  against, on the rubrics the framework ships against; carrying them
  in-tree costs parity-corpus maintenance the empirical evidence does
  not warrant.
- **Keep only `single_shot_rubric` and drop the rest.** Rejected:
  `voted_rubric` and `auto_anchored_rubric` each measured a real drift
  reduction on shipped rubrics, and the composed recipe closes both
  measured drift classes. Removing them would push every downstream
  package that needs either recipe out of the registry, defeating
  ADR-0046's plug-in aim from the opposite direction.

## Cross-references

- Supersedes: [ADR-0046 — The `JudgeKind` Protocol and Entry-Point Registry](0046-judgekind-protocol-and-registry.md).
- Extends: [ADR-0043 — Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract](0043-detached-mode-grader-and-typed-grader-kinds.md).
- `JudgeKind` authoring guide, parity gate, and the three shipping kinds: [`docs/JUDGE_KINDS.md`](../JUDGE_KINDS.md).
- Grade bundle recording of `judge_kind` / `kind_config`: [`docs/GRADE_BUNDLE.md`](../GRADE_BUNDLE.md).
- Grading dispatch: [`docs/GRADING.md`](../GRADING.md).
- Milestone: 51 (registry consolidation pass).

## Update: 2026-09-23

Live regression evidence on three real M51 bundles confirmed that
`auto_anchored_rubric` diverges from `single_shot_rubric` on unanchored
graded criteria (`clarity` example: `single_shot` deterministic 0.0,
`auto_anchored` 0.167–0.333 across bundles), driven by a warm-up prompt
that produced anchors *looser* than the criterion description. Rather
than exposing more composition knobs to users, we simplified the
user-facing surface to a single 3-value enum and baked the best-measured
composition in:

- **User-facing `judge_kind:` values are now exactly three**:
  `single_shot_rubric`, `multi_turn_rubric`, `auto_rubric`. No other
  value is a supported user selection.
- `multi_turn_rubric` bakes the composition
  `voted(n=3, geometric_median) → auto_anchored → single_shot` in
  with no user knobs. Any non-empty `kind_config` raises `ValueError`.
- `auto_rubric` inspects the rubric shape at grade time and routes
  deterministically: to `multi_turn_rubric` when any graded criterion
  has `expected: None`, otherwise to `single_shot_rubric`. No user
  knobs; the selection reason lands as a prefix on
  `JudgeResult.reasons`.
- `voted_rubric` and `auto_anchored_rubric` remain in-tree and
  registered as internal building blocks so `multi_turn_rubric` can
  compose them and downstream packages that want alternative
  compositions have building blocks to reach for. Their entry-point
  registrations are preserved to keep the reinstated parity coverage
  green. User-facing docs now describe them under an
  "internal building blocks" heading and note that user code should
  not select them via `judge_kind:` directly.
- The `auto_anchored` warm-up prompt was tightened to bind anchors to
  be at least as strict as the criterion description, to explicitly
  reject non-responsive outputs (empty replies, error messages,
  off-topic content, hedged non-answers, generic filler), and to name
  concrete observable evidence. When the rubric carries a `reference`,
  the warm-up user prompt now prepends a reference-solution calibration
  block so the anchor snaps toward the task author's ground truth.
