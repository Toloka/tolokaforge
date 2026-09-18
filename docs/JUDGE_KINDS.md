# JudgeKind — pluggable LLM-judge dispatch

## Overview

The `JudgeKind` seam is a pluggable dispatch layer for LLM-as-judge
grading. A `JudgeKind` implementation resolves a rubric + transcript
into a `JudgeResult`; the runner selects one per task via
`grading.llm_judge.judge_kind` and passes per-kind options through
`grading.llm_judge.kind_config`. The seam contract lives at
[`tolokaforge/core/grading/judge_kinds/_protocol.py`](../tolokaforge/core/grading/judge_kinds/_protocol.py);
the seam surface (loader, entry-point group, per-task selection) is
documented in [`docs/GRADER_SERVICE.md § Sub-component plug-in seams`](GRADER_SERVICE.md#sub-component-plug-in-seams);
the composite fold that dispatches into the kind is documented in
[`docs/GRADING.md`](GRADING.md).

Four kinds ship in the reference distribution: `single_shot_rubric`
(wraps `LLMJudge` in one shot, byte-identical with the pre-seam
`LLMJudgeRubricEvaluator`), `chunked_rubric` (one `LLMJudge`
invocation per chunk of the rubric's criteria, optionally grouped by
`Criterion.chunk_group` — the opt-in kind for large rubrics where a
single `submit_report` payload would exceed the judge model's
output-token ceiling), `voted_rubric` (wraps any
registered kind and samples it K times, folding the per-criterion
verdicts through a robust aggregator to reduce judge-model
self-variance — see § Voted kind), and `jury_rubric` (wraps any
registered kind and dispatches to a cross-family panel of N different
judge models instead of K samples of one model, folding the
per-criterion verdicts through the same robust aggregator — see § Jury
kind). Downstream packages register further alternatives (e.g.
agentic) alongside without a framework PR.

The `JudgeKind` Protocol and registry seam decision is recorded in
[`docs/adr/0046-judgekind-protocol-and-registry.md`](adr/0046-judgekind-protocol-and-registry.md).

## Protocol contract

Every `tolokaforge.judge_kinds` entry resolves to a class satisfying
[`JudgeKind`](../tolokaforge/core/grading/judge_kinds/_protocol.py), a
`runtime_checkable` `Protocol`:

```python
@runtime_checkable
class JudgeKind(Protocol):
    NAME: ClassVar[str]

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None,
        kb_search: KnowledgeSearch | None,
        workspace_dir: Path | None,
        extra_read_tools: list[Tool],
        state_diff: str | None,
        judge_model_config: ModelConfig,
        judge_model_provider: JudgeModelProvider,
        disable_knowledge_search: bool,
        custom_system_prompt: str | None,
        include_agent_system_prompt: bool,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult: ...
```

**`NAME`** MUST equal the entry-point name the kind registers under — a
`pyproject.toml` typo surfaces at discovery time (`load_judge_kind`
raises), never silently at judge time.

**`evaluate` is kwargs-only.** Every field on the per-trial evidence
surface (`rubric` through `state_diff`) mirrors `LLMJudge.run`'s own
inputs verbatim, so a kind that just wraps `LLMJudge` (`single_shot_rubric`)
needs no translation layer. `judge_model_config` + `judge_model_provider`
are construction inputs — the kind builds its own judge client(s) from
them, once or many times per `evaluate` call. `disable_knowledge_search`,
`custom_system_prompt`, and `include_agent_system_prompt` are per-trial
customization every kind must honor identically to `LLMJudge`.

**`kind_config: Mapping[str, Any] | None`** is an opaque bag the Protocol
itself does not interpret — each kind owns its own schema and validation.
The shipping pattern (`chunked_rubric`) is a
module-level `frozenset` of accepted keys, a `frozen @dataclass` holding
the resolved, typed config, and a `_resolve_kind_config` function that
raises `ValueError` naming the kind, the bad key, and the accepted set on
any unknown key or a wrong-typed/out-of-range value — validated eagerly,
before any judge dispatch or `judge_model_provider.build()` call runs. A
kind that takes no options (`single_shot_rubric`) still receives
`kind_config` on the Protocol and explicitly discards it
(`del kind_config  # reserved on the Protocol for downstream kinds`)
rather than silently ignoring an unused parameter.

**Return contract.** `evaluate` MUST produce a `JudgeResult`. A judge
malfunction — a malformed `submit_report` past its retry budget, turn or
wall-clock budget exhaustion, or any loop-terminal exception — surfaces
as `JudgeStatus.ERRORED` with `score=None` and `criterion_results=()`,
never a `0.0`/`0.5` fallback and never a partial verdict for a subset of
criteria. Every shipped kind reuses the same `parse_submit_report` /
`aggregate_rubric` validation, so this contract holds identically across
kinds — see the fail-loud paragraphs under § Chunked kind for the
non-trivial case (a bad chunk).

## Authoring a new judge kind

1. **Implement the Protocol.** Write a class with
   `NAME: ClassVar[str]` and an `evaluate(...)` method matching the
   signature in § Protocol contract above. If your kind takes options,
   resolve `kind_config` eagerly at the top of `evaluate` into your own
   validated, typed shape — raise `ValueError` on anything unrecognised,
   before touching `judge_model_provider`.
2. **Register the entry point.** Add one line to your package's
   `pyproject.toml` under `[project.entry-points."tolokaforge.judge_kinds"]`:
   `your_kind_name = "your_package.module:YourJudgeKind"`. `NAME` and the
   entry-point name must match — mismatches are caught at discovery.
3. **Add corpus fixtures.** Every fixture under
   `tests/data/judge_kind_parity_corpus/` needs a cassette for your kind
   — `judge_scripts.<your_kind_name>` for a kind that builds one judge
   client per `evaluate` call, or `judge_scripts_per_chunk.<your_kind_name>`
   for a kind that builds N. See § Adding a new judge kind (under
   § Parity gate below) for the exact registration mechanics and
   § Corpus authoring rules for the per-fixture cassette shape.
4. **Clear the parity gate.** Run the canonical parity lane
   (`tests/canonical/test_judge_kind_parity.py`) with your kind named in
   its parametrise list. Every criterion must land `pass` or `warn` under
   both cross-kind agreement (vs. `single_shot_rubric`) and
   self-consistency (§ Three-level thresholds) before the kind is
   default-eligible for any task pack.
5. **Document the kind.** Add a `## <Your kind> kind` section to this
   file, in the shape of § Chunked kind (config schema, fail-loud
   behaviour, any state machine or persistence notes), and a short entry
   under § Worked examples.

## Worked examples

One minimal `grading.llm_judge` snippet per shipped kind. Each cross-refers
its detailed section rather than repeating it.

### `single_shot_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: single_shot_rubric
```

No `kind_config` — the kind receives it on the Protocol and discards it
unread. One `LLMJudge` call produces the whole rubric's verdict in a
single `submit_report`; this is the default, byte-identical with the
pre-seam evaluator, and every task pack that predates the `JudgeKind`
seam runs this kind unchanged.

### `chunked_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: chunked_rubric
    kind_config:
      chunk_size: 8
```

Splits the rubric into contiguous 8-criterion chunks, runs one
`LLMJudge` per chunk against a scoped sub-rubric, and folds the merged
per-criterion results through `aggregate_rubric` on the original rubric.
Opt in for rubrics whose single `submit_report` payload would overflow
the judge model's output-token ceiling — see § Chunked kind for the
fail-loud and persistence contract.

### `voted_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: voted_rubric
    kind_config:
      n_samples: 3
      aggregator: geometric_median
      wrapped_kind: single_shot_rubric
```

Runs `single_shot_rubric` (or any other registered kind named by
`wrapped_kind`) three times against the same rubric evidence and folds
the three per-criterion verdicts through the geometric-median
aggregator. Opt in to reduce judge-model self-variance on
subjective/graded criteria — see § Voted kind for the config schema,
the fail-loud contract, and the aggregator trade-offs.

### `jury_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: jury_rubric
    kind_config:
      panel:
        - {provider: openrouter, name: openai/gpt-4o-mini, temperature: 0.0}
        - {provider: openrouter, name: anthropic/claude-3-haiku, temperature: 0.0}
        - {provider: openrouter, name: google/gemini-2.0-flash, temperature: 0.0}
      aggregator: geometric_median
      wrapped_kind: single_shot_rubric
```

Runs `single_shot_rubric` (or any other registered kind named by
`wrapped_kind`) once per panel member — each member a DIFFERENT judge
model, not a repeated sample of one model — and folds the per-criterion
verdicts through the same geometric-median aggregator `voted_rubric`
uses. Opt in when cross-family model diversity outweighs the cost of a
same-model K-sample vote (PoLL evidence) — see § Jury kind for the
config schema, the credential-preflight contract, and the fail-loud
contract.

## Chunked kind

`chunked_rubric` partitions the rubric's criteria into chunks of at
most `chunk_size` criteria — first grouping criteria that share a
`Criterion.chunk_group` name (in first-appearance order) into one
block, then packing every block in order into chunks of size
`chunk_size`, with a group whose own size exceeds `chunk_size` spanning
consecutive chunks on its own — runs one `LLMJudge` per chunk against
a scoped sub-rubric (each chunk sees the original `reference`
verbatim), and merges the per-chunk `CriterionResult` maps into the
original full rubric — folded through `aggregate_rubric` on the
original rubric so `score` / `binary_pass` / `gate_failed` come out of
the same math the single-shot kind uses. Opt in via
`grading.llm_judge.judge_kind: chunked_rubric`; the default remains
`single_shot_rubric`.

`kind_config` schema: `{"chunk_size": int}`. `chunk_size` must be `>= 1`
(a `chunk_size >= len(criteria)` degenerates to a single call, which is
deliberate). When `kind_config` omits `chunk_size` (or is itself
`None`), the effective size is derived from the judge model's
output-token headroom: `max(1, floor(max_tokens * 0.6 / 200))`, where
`max_tokens` is read off `judge_model_config.max_tokens`, `200` is the
per-criterion verdict token estimate
(`TOKENS_PER_CRITERION_ESTIMATE`), and the `0.6` factor
(`HEADROOM_FRACTION`) reserves 40 % of `max_tokens` for the judge's
reasoning tokens and a retry buffer. When `max_tokens` is unset
(`None`), a conservative `FALLBACK_MAX_TOKENS = 2048` stands in
(yielding six criteria per chunk on the fallback path). This lets a
large-context judge degenerate to a single call when the whole rubric
fits in headroom while still chunking large rubrics against the
truncation failure class the kind exists to remove. All three
constants are module-level in
[`chunked.py`](../tolokaforge/core/grading/judge_kinds/chunked.py) so a
downstream deployment can monkeypatch them without adding new
`kind_config` plumbing; per-model conditional branching is deliberately
absent (one estimator across every judge model). Any unknown key or a
non-positive `chunk_size` raises `ValueError` inside `evaluate` before
any judge call runs.

### chunk_group grouping

Each `Criterion` may declare a free-form `chunk_group: <name>` (default
`None`). `chunked_rubric` groups criteria sharing the same name into
the same chunk (or, when the group exceeds `chunk_size`, consecutive
chunks that hold only that group's criteria) before packing everything
else in first-appearance order. The algorithm is deterministic and
pure:

1. **Group blocks, first-appearance order.** Walk `rubric.criteria`
   once. A criterion with `chunk_group is None` becomes its own
   singleton block anchored at its position. A criterion with
   `chunk_group = "x"` joins block `"x"`, anchored at `"x"`'s
   first-occurrence position; non-contiguous same-group criteria are
   silently pulled together at that anchor.
2. **Pack blocks into chunks of ≤ `chunk_size`.** Walk the ordered
   blocks. A block that fits in the current chunk's remaining room is
   appended. A block that does not fit but is itself `<= chunk_size`
   flushes the current chunk and starts a new one with that block. A
   block whose own size exceeds `chunk_size` flushes the current chunk,
   then is sliced on its own into consecutive `chunk_size`-runs (never
   combined with another block).

Worked example — a 5-criterion hotel-review rubric with three declared
groups (`wifi`, `staff`, `food`) at `chunk_size = 3`:

```yaml
criteria:
  - id: wifi_speed
    chunk_group: wifi
  - id: food_variety
    chunk_group: food
  - id: wifi_reach
    chunk_group: wifi
  - id: staff_polite
    chunk_group: staff
  - id: food_hot
    chunk_group: food
```

Phase 1 groups blocks by first-occurrence anchor: `wifi` block
`[wifi_speed, wifi_reach]`, `food` block `[food_variety, food_hot]`,
`staff` block `[staff_polite]`. Phase 2 packs in order: `wifi` (2)
plus `food` (2) overflows chunk 0 (2 + 2 > 3) → chunk 0 =
`[wifi_speed, wifi_reach]`; `food` (2) plus `staff` (1) fits →
chunk 1 = `[food_variety, food_hot, staff_polite]`.

Oversize group example — 8 criteria sharing one group, `chunk_size = 5`:
the group's own size (8) exceeds `chunk_size`, so it flushes into
consecutive chunks of shape `[5, 3]` on its own, and no other group's
criterion joins either slice.

`chunk_group` names are free-form and scoped to the rubric they're
declared on — the same name in another task's rubric means nothing.
Rubrics that declare no `chunk_group` degenerate to plain fixed-K runs
identical to `criteria[i : i + chunk_size]` slicing — the byte-parity
anchor the κ-parity gate depends on. Reordering happens inside
`_chunk_boundaries` only; `_merge_chunk_results` re-indexes
`criterion_results` back to `rubric.criteria`'s original order, so a
rubric's final `criterion_results` order is unaffected by grouping —
only which criteria share a judge call.

Per-chunk fail-loud (#1471): any chunk whose `JudgeResult.status` is
not `COMPLETED` — or whose `criterion_results` is missing one of its
chunk's criterion ids — yields a whole-trial `JudgeResult` with
`status=ERRORED`, `score=None`, `criterion_results=()`, and a `reasons`
naming the failing chunk index + its criterion ids + the underlying
reason. `chunk_boundaries` is still populated with every boundary
attempted, so `build_replay_grade` and every other `Grade`-writing path
persist them (see § Persistence below) and offline replay can retry
only the failing chunk. There is never a silent partial-rubric score.

### Persistence

Chunk boundaries land on `Grade.judge_chunk_boundaries` (inline in
`grade.yaml` as a list-of-lists of criterion ids in original rubric
order), populated by every path that produces a `Grade` from a
`JudgeResult`: the runner-service composite, the grader-service
composite, `CompositeGraderKind._recompute_from_substrate` (offline
regrade via `tolokaforge grade`), and `build_replay_grade` (the
judge-only + `replay.replay_trial` seam). `None` when no judge ran or
when a non-chunking kind produced the grade; a non-empty list otherwise
— even on a whole-trial ERRORED chunked run (every boundary attempted
is recorded, per the fail-loud contract, so an offline replay can retry
the failing chunk without re-planning boundaries).

Wire: field 16 `string chunk_boundaries_json` on both `runner.proto` and
`grader.proto`'s `JudgeReport`, JSON-encoded as `[[criterion_id, ...],
...]`. Empty string is the proto3 default and the "no chunking" wire
encoding — the host materialiser maps it to `None`.

Bundle-side: `chunk_boundaries` is a judge OUTPUT, not a grading INPUT,
so it lives on the grade side (`grade.yaml`), not on the v1.1 bundle.
The bundle's `grading_config.json` records `kind_config` and the
recorded `judge_model_config.json` (its `max_tokens` feeds the adaptive
`chunk_size` when `kind_config` omits one) — the chunked kind re-derives
the same boundaries deterministically on regrade from either the
explicit `kind_config.chunk_size` or the same adaptive computation.

### Replay routing

Offline replay (`tolokaforge.core.grading.replay::replay_trial`)
dispatches through `load_judge_kind(inputs.judge_kind)()` — the same
seam the runner-side composite, the grader-service composite, the
offline `CompositeGraderKind` recompute, and `judge_only_helpers` all
use. `inputs.judge_kind` + `inputs.kind_config` are resolved from the
bundle's `task.yaml.grading_config.llm_judge` at
`read_replay_inputs` time. A recorded trial with
`judge_kind: chunked_rubric` + `kind_config: {chunk_size: N}` replays
through `ChunkedRubricJudgeKind`; a legacy trial artifact predating
[#1567][pr-1567] lacks both fields and defaults to
`("single_shot_rubric", None)` — byte-identical to prior behaviour, and
the byte-parity anchor `tests/canonical/test_judge_kind_single_shot_byte_parity.py`
guards it. `ReplayProvenance.judge_kind_source` stamps the origin
(`RECORDED` — no CLI `--judge-kind` override; kind A/B comparison lives
in the parity harness, not on the offline replay CLI).

The bundle-branch prompt escape hatch: when the bundle recorded a
composed judge prompt via `prompts.yaml.judge_prompt`
(`inputs.explicit_system_prompt` is set), `replay_trial`
short-circuits to a direct `LLMJudge` construction —
`JudgeKind.evaluate` has no `explicit_system_prompt` kwarg today, and
the recorded composed prompt supersedes both the task customization
and the kind's default composition. The escape hatch keeps
`test_bundle_judge_prompt_persistence.py` green and is directly locked
by `test_replay_bundle_branch_bypasses_kind_seam.py`. Widening
`JudgeKind.evaluate` with an optional `explicit_system_prompt`
keyword-only argument is tracked at [#1583][issue-1583]; when that
lands the short-circuit disappears and every bundle-branch trial
re-routes through the seam.

[pr-1567]: https://github.com/Toloka/tolokaforge/pull/1567
[issue-1583]: https://github.com/Toloka/tolokaforge/issues/1583

Cost note: a rubric split into N chunks consumes up to `N ×` the
single-shot per-trial wall-clock and system-prompt tokens. This is the
acknowledged cost of removing the truncation failure class; the trade
between chunk size and reliability is measured in follow-up #1581.

## Voted kind

`voted_rubric` wraps any other registered `JudgeKind` (default
`single_shot_rubric`), calls its `evaluate` `n_samples` times (default
3) against the SAME rubric evidence — the wrapped kind always receives
`kind_config=None`, so a nested config on the wrapped kind (e.g. a
non-default `chunk_size` on a wrapped `chunked_rubric`) is not
supported — and folds the K per-criterion verdicts through a robust
aggregator (`tolokaforge.core.grading.judge_kinds.aggregators`) before
re-folding the merged results through `aggregate_rubric` on the
original rubric, exactly as every other kind does. Opt in via
`grading.llm_judge.judge_kind: voted_rubric`; the default remains
`single_shot_rubric`.

`kind_config` schema: `{"n_samples": int, "aggregator": "majority" |
"median" | "geometric_median", "wrapped_kind": str}`. All three keys
are optional — `n_samples` defaults to 3, `aggregator` defaults to
`geometric_median`, `wrapped_kind` defaults to `single_shot_rubric`.
`n_samples` must be a non-`bool` `int` `>= 2` (K<2 makes voting
undefined). `aggregator="majority"` additionally requires an odd
`n_samples` (no implicit tie-break) and an all-`binary` rubric (a
majority vote over a graded 0–1 criterion has no defined threshold).
`wrapped_kind` must resolve via `load_judge_kind` — an unknown name
raises the registry's own `UnknownImplementationError` naming the
registered set. Any unknown `kind_config` key, or a violation of the
above, raises `ValueError` before any judge dispatch runs.

**Aggregators:**

- `"median"` — per-criterion `statistics.median` over the K samples'
  scores for that criterion, independently per criterion.
- `"majority"` — per-criterion boolean vote at the 0.5 threshold
  (requires an odd `n_samples` and an all-binary rubric, enforced
  above).
- `"geometric_median"` (default) — treats each sample as ONE point in
  R^n_criteria (a full per-sample verdict vector) and returns the point
  minimizing the sum of Euclidean distances to the K sample points
  (Weiszfeld/Vardi-Zhang, `MAX_ITERATIONS = 100`,
  `CONVERGENCE_TOLERANCE = 1e-8`). This is what makes it distinct from
  `"median"`: a sample whose ENTIRE verdict set is contaminated
  (sycophancy, mode collapse) is down-weighted as a unit rather than
  diluted criterion-by-criterion. Fails loud with `RuntimeError` naming
  the iteration cap, the tolerance, and the last iterate if the
  algorithm does not converge — never a silent stale midpoint.

Per-sample fail-loud (mirrors `chunked_rubric`'s per-chunk contract,
renamed to per-sample): any sample whose `JudgeResult.status` is not
`COMPLETED` — or whose `criterion_results` is missing one of the
rubric's criterion ids — yields a whole-trial `JudgeResult` with
`status=ERRORED`, `score=None`, `criterion_results=()`, and a `reasons`
naming the failing sample index and the underlying reason. Iteration
stops at the first failing sample (later samples are never dispatched);
`usage` is still summed across every sample that DID dispatch.

**Justification audit trail.** Each merged `CriterionResult.justification`
records the aggregator name, K, the raw per-sample scores for that
criterion, the resulting aggregate, and then every sample's own
justification labelled by index — so a reviewer can see exactly which
sample(s) drove (or were down-weighted out of) the final verdict.

`chunk_boundaries` is always `()` — `voted_rubric` never chunks, so
persistence and replay treat it exactly like `single_shot_rubric` for
that field.

Cost note: K samples consume up to `K ×` the wrapped kind's per-trial
wall-clock, tokens, and cost. This is the acknowledged cost of reducing
judge-model self-variance; the K vs. reliability trade is measured in
the umbrella issue's live A/B report (see § Live A/B below).

## Jury kind

`jury_rubric` wraps any other registered `JudgeKind` (default
`single_shot_rubric`) exactly like `voted_rubric`, but where
`voted_rubric` samples ONE model K times, `jury_rubric` dispatches to a
cross-family PANEL of N DIFFERENT judge models — each panel member
supplies its own `provider`/`name` (and optional `temperature`), so a
task author gets model diversity instead of repeated-sampling variance
reduction from the same model. The wrapped kind always receives
`kind_config=None` on every panel dispatch, same restriction as
`voted_rubric`. Opt in via `grading.llm_judge.judge_kind: jury_rubric`;
the default remains `single_shot_rubric`.

`kind_config` schema: `{"panel": list[{"provider": str, "name": str,
"temperature": float}], "aggregator": "majority" | "median" |
"geometric_median", "wrapped_kind": str}`. All three keys are optional.
`panel` defaults to a 3-member cross-family panel, all routed through
OpenRouter: `openai/gpt-4o-mini`, `anthropic/claude-3-haiku`, and
`google/gemini-2.0-flash`, each at `temperature: 0.0`. Every panel
entry requires a non-empty `provider` and `name`; `temperature` is
optional and must be a non-`bool` `int`/`float` when present; any
unrecognised entry key raises `ValueError` naming the entry index.
`aggregator` defaults to `geometric_median` and `wrapped_kind` defaults
to `single_shot_rubric`, with the same validation `voted_rubric` uses
(§ Voted kind) — panel size stands in for `n_samples`: `len(panel) >= 2`
(K<2 makes voting undefined), and `aggregator="majority"` additionally
requires an odd panel size and an all-`binary` rubric. Any unknown
top-level `kind_config` key, or a violation of the above, raises
`ValueError` before any judge dispatch runs.

**Credential preflight.** Before any panel member is dispatched, every
DISTINCT `provider` across the panel is checked against
`tolokaforge.core.llm.providers.credential_env_names` and
`SecretManager.has_secret` — a provider whose every candidate
credential name is absent accumulates into ONE `ValueError` naming
EVERY missing provider at once (not just the first), since a task
author fixing panel credentials wants the whole list in one pass. A
provider with no known credential-name mapping (an out-of-tree
provider `credential_env_names` cannot resolve) is skipped —
preflighting it is impossible, so it fails loud at the LLM call itself
instead, exactly as it does today without `jury_rubric`.

Reuses `tolokaforge.core.grading.judge_kinds.aggregators` unchanged —
the same `"median"` / `"majority"` / `"geometric_median"` aggregators
`voted_rubric` uses, with identical semantics (§ Voted kind lists them).

Per-member fail-loud (mirrors `voted_rubric`'s per-sample contract,
renamed to per-panel-member): any panel member whose
`JudgeResult.status` is not `COMPLETED` — or whose `criterion_results`
is missing one of the rubric's criterion ids — yields a whole-trial
`JudgeResult` with `status=ERRORED`, `score=None`,
`criterion_results=()`, and a `reasons` naming the failing member's
INDEX plus its `provider`/`name` — panel members are heterogeneous, so
naming which model failed is the point, not just which position in the
list. Iteration stops at the first failing member (later members are
never dispatched); `usage` is still summed across every member that DID
dispatch.

**Justification audit trail.** Each merged `CriterionResult.justification`
records the aggregator name, N, the raw per-member scores for that
criterion, the resulting aggregate, and then every member's own
justification labelled by its panel `provider/name` (not a bare index,
since a reviewer needs to know WHICH model produced which verdict in a
heterogeneous panel).

`chunk_boundaries` is always `()` — `jury_rubric` never chunks, same as
`voted_rubric`.

Cost note: N panel members consume up to `N ×` the wrapped kind's
per-trial wall-clock, tokens, and cost — the same acknowledged
multiplier `voted_rubric`'s `K ×` carries. The trade `jury_rubric`
offers over `voted_rubric` is diversity, not a cheaper cost model: pick
`voted_rubric` when the goal is damping one model's own self-variance
cheaply, and `jury_rubric` when cross-family diversity outweighs that
cost per PoLL (Panel of LLM evaluators) evidence.

## Composing kinds

`voted_rubric` and `jury_rubric` both take a `wrapped_kind` field on
`kind_config`; the wrapped kind is dispatched once per sample / panel
member. Composition is a first-class extension point:

- **`voted_rubric` wrapping `chunked_rubric`** — K samples of the whole
  rubric, each sample itself chunked into ≤ `chunk_size` criterion
  groups. Fits when the rubric is large AND you want K-sample
  variance reduction on top. Cost = K × chunked's per-trial cost.

- **`jury_rubric` wrapping `chunked_rubric`** — a cross-family panel
  where each member's grade is itself chunked. This is the
  recommended composition for `jury_rubric` on rubrics of ≥ 6
  criteria: the weakest panel member (typically the smallest
  cheap-tier model) is the truncation floor for the whole panel, and
  wrapping it in `chunked_rubric` narrows each panel-member call to a
  sub-rubric that fits well inside every family's output-token
  ceiling. Without this wrapping, a single member's truncated
  `submit_report` on a large rubric fails the whole panel loud per
  `jury_rubric`'s per-member contract. Example:

  ```yaml
  grading:
    llm_judge:
      judge_kind: jury_rubric
      kind_config:
        wrapped_kind: chunked_rubric
  ```

Wrapped-kind ergonomics: the wrapper always passes `kind_config=None`
into the wrapped kind, so the wrapped kind uses its own defaults —
`chunked_rubric`'s adaptive `chunk_size` heuristic (per this milestone)
picks the effective chunk size from the judge model's `max_tokens`
headroom, so no explicit `chunk_size` needs to be threaded through the
outer composition.

## Parity gate

Every `JudgeKind` — the shipped `single_shot_rubric` and every
downstream kind that joins the registry — proves its per-criterion
verdicts against the reference kind (`single_shot_rubric`) and against
itself across replays before it can ship as a task default. The gate
lives at
[`tests/canonical/test_judge_kind_parity.py`](../tests/canonical/test_judge_kind_parity.py);
the measurement harness that produces its inputs is
[`tolokaforge/core/grading/judge_kinds/parity.py`](../tolokaforge/core/grading/judge_kinds/parity.py).

### What the gate measures

Two measurements, both per-criterion, both anchored on the same 20
committed cassette fixtures under
[`tests/data/judge_kind_parity_corpus/`](../tests/data/judge_kind_parity_corpus/):

- **Cross-kind agreement** — `measure_cross_kind_agreement` drives
  the candidate kind and `single_shot_rubric` over the same corpus,
  pairs their per-criterion verdicts, and computes Cohen's κ per
  criterion. A candidate that disagrees with the reference below the
  block bar on any criterion cannot ship as a task default.
- **Self-consistency** — `measure_self_consistency` runs the kind
  over the corpus five times (five fresh instances from
  `kind_factory(replay_index)` against five fresh providers from
  `provider_factory(replay_index)`), pairs every replay against
  replay 0, and computes per-criterion κ over the pooled pairs. A
  kind whose verdicts drift across replays below the self bar is not
  shippable.

Both measurements return a
`tolokaforge.core.grading.agreement.CalibrationReport` — the same
shape the calibrator produces, so callers reason about parity and
calibrator agreement with the same tooling.

### Three-level thresholds

`ParityGateThresholds` (Landis-Koch anchored; aligns with MT-Bench /
Langfuse's 80 % judge-agreement bar):

| Field | Default | Meaning |
| --- | --- | --- |
| `block` | 0.8 | Cross-kind pass bar (κ ≥ this ships). |
| `self_consistency_block` | 0.7 | Self-consistency pass bar (κ ≥ this ships). |
| `warn` | 0.6 | Advisory floor — the "block-if-below" bar in both measurements. |

`decide_parity_gate` walks each criterion in the report and picks one
of four verdicts:

- `pass` — `κ ≥ pass_bar` (shippable).
- `warn` — `warn ≤ κ < pass_bar` (shippable; the id lands in
  `warning_criteria` so a reviewer sees the near-miss).
- `block` — `κ < warn` (not shippable; id lands in
  `blocking_criteria`).
- `insufficient_evidence` — κ is undefined (fewer than two paired
  observations for that criterion, or a label-invariant pool where
  chance agreement is total). Not shippable; the reason quotes
  `"undefined"` verbatim so a grep-based CI parser catches it.

Aggregate `shippable` is `all(status in {pass, warn})` — the gate
never rolls per-criterion κ into a single number.

### Partial verdicts fail loud

An entry whose reference or candidate leg returns a `JudgeResult`
with `criterion_results` shorter than the rubric's criterion count
(partial completion, `status == ERRORED`, chunked kind failing on
one chunk) contributes NO paired observation and lands in the
report's `errored_fixture_ids` with a reason naming the entry_id,
the leg that fell short, and the missing criterion ids. A candidate
that emits verdicts for 4 out of 5 criteria is not "80 % agreeing" —
it is failing to grade one criterion, and the gate reports it as
such.

### Cassette mode vs live mode

The canonical lane runs entirely from committed cassettes. Every
corpus fixture's `judge_scripts.<kind_name>` block is a scripted
sequence of judge-loop turns; the loop's `LLMClient` is monkeypatched
to a `ScriptedLLMClient` that replays those turns deterministically.
The lane runs keyless, network-free, and under a hard runtime budget
(inner-sum < 60 s, full wall-clock < 90 s), so it stays cheap enough
for every CI run.

`pytest --live-parity` opts into live mode: passing the flag requires
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the environment, then drives
every corpus entry against a real `LiteLLMJudgeModelProvider`-backed
`RecordingLLMClient` for every kind under test, and writes the recorded
script back into the originating
`tests/data/judge_kind_parity_corpus/**/*.yaml` fixture's
`judge_scripts[kind_name]` (or `judge_scripts_per_chunk[kind_name]` for a
multi-client kind) — every other key in the fixture file is preserved
byte-identical. This is the cassette-refresh mechanism: it keeps the
20-fixture corpus in sync with what a shipped kind's real judge model
actually says today, so the cassette-mode lane above keeps replaying a
faithful script. It is a distinct mechanism from § Live A/B below, which
measures cross-kind agreement on real trials rather than refreshing this
fixed corpus. CI never passes `--live-parity` — refreshing cassettes is a
manual step run against real budget when a kind's prompt or behaviour
changes.

### Adding a new judge kind

Register the kind in your package's `pyproject.toml` under
`[project.entry-points."tolokaforge.judge_kinds"]`, add a cassette
block for every corpus fixture under `judge_scripts.<your_kind_name>`,
name your kind in the parametrise list of
[`tests/canonical/test_judge_kind_parity.py`](../tests/canonical/test_judge_kind_parity.py),
and iterate on the kind until every per-criterion verdict lands as
`pass` or `warn`. A missing cassette for a kind under test raises a
`KeyError` at lane collection naming the entry_id and the missing
kind — silent skips are the failure mode the contract refuses.

**Kinds that need N clients per fixture.** A kind that calls
`judge_model_provider.build(...)` more than once per `evaluate` (the
chunked kind is the shipping example) authors its cassette under
`judge_scripts_per_chunk.<your_kind_name>` — a list of scripts, one per
`build` call. The parity lane's pool provider dispatches on cassette
shape (presence of `judge_scripts_per_chunk[NAME]`), so no harness
change is needed: pop the N scripts as N `ScriptedLLMClient` instances
for that fixture. See the `chunked_rubric` cassettes under
`tests/data/judge_kind_parity_corpus/large_rubrics/` for the multi-chunk
authoring shape (three `-` levels: per-chunk scripts list → the script's
single turn → the turn's single tool call). A kind that calls
`judge_model_provider.build(...)` exactly once per `evaluate` (the
shipping example: `single_shot_rubric`) authors its cassette under
`judge_scripts.<your_kind_name>` instead — a single-client,
possibly-multi-turn script.

### Corpus authoring rules

The 20 committed fixtures split across three shape families to
exercise representative judge dispatch:

- 10 × `small_rubrics/` — 2–4 criteria, single-turn transcript.
- 6 × `large_rubrics/` — 8–15 criteria, single-turn transcript.
- 4 × `multi_turn/` — 2–4 criteria, 3–5-turn transcript.

Every criterion id in the corpus MUST appear in at least two
fixtures with mixed True/False verdicts. A criterion that always
returns the same label pool-wide produces `κ = undefined` (chance
agreement is total, so the denominator collapses) and the gate
reports `insufficient_evidence` for it — blocking the lane. When
adding a new criterion, add it to enough fixtures with mixed
verdicts to keep its per-criterion pool label-variant.

Every fixture is one `entry.yaml` file in `ParityCorpusEntry` shape:
`{entry_id, rubric, agent_system_prompt, transcript, state_diff,
disable_knowledge_search, custom_system_prompt,
include_agent_system_prompt, judge_scripts, judge_scripts_per_chunk}`.
The `judge_scripts.<kind_name>` value is a list of turns; each turn is
either a string (assistant text) or a list of tool-call dicts
(`{name, arguments}`). The `single_shot_rubric` cassette is one turn
calling `submit_report` with per-criterion verdict + justification
args; the justification MUST use YAML double-quoted syntax so `\n`
is interpreted as a real newline (the judge's verdict-consistency
regex needs the marker on its own line).

Kinds that dispatch a fresh client per chunk (`chunked_rubric`) author
their cassette under `judge_scripts_per_chunk.<kind_name>` — a
list of scripts, one per chunk. Every fixture MUST ship a
`judge_scripts_per_chunk.chunked_rubric` block with
`ceil(len(criteria) / chunk_size)` scripts at `chunk_size = 5` — the
`test_corpus_has_twenty_entries` loader lock asserts the count.
Small-rubric and multi-turn fixtures (2–4 criteria) ship a
single-script cassette (single-chunk degenerate case); large-rubric
fixtures (8–15 criteria) ship 2–3 scripts. Per-criterion verdicts
across the chunk scripts MUST match the single-shot cassette's for
identical cross-kind κ.

## Live A/B: cross-kind κ and cost on real trials

The 20-fixture corpus above proves a candidate kind agrees with the
reference on synthetic, hand-authored transcripts. It cannot answer
whether that agreement holds on real task variety, nor what a kind
actually costs per trial. `tools/judge-kind-ab` answers both: it drives
the same κ-agreement harness (`measure_cross_kind_agreement`,
`measure_self_consistency`) against a live judge model over real
completed trials instead of committed cassettes.

It assembles one `ParityCorpusEntry` per completed trial's grade bundle
(`judge_kind_ab.bundle_corpus.corpus_entry_from_bundle` /
`load_corpus_from_run`), reading `grading_config.json`,
`task_description.json`, and `trajectory.json` the same way
`CompositeGraderKind.evaluate` does for offline regrade — see
[`docs/GRADE_BUNDLE.md`](GRADE_BUNDLE.md). For every unordered pair of
the kinds under test it measures cross-kind agreement; for every kind it
measures self-consistency across replays; then it renders a
per-criterion κ table (annotated via `decide_parity_gate`, § Three-level
thresholds) and a per-task-family cost table (calls, tokens, `cost_usd`,
from `JudgeUsage` aggregation).

CLI:

```
judge-kind-ab run <bundles-dir> \
  --model-ref <provider/model> \
  --kinds single_shot_rubric,chunked_rubric \
  --replays 5 \
  --out-dir <dir>
```

The command always exits 0 — a below-threshold κ is an annotation on the
rendered table, not a hard failure; this is a one-off evidence-gathering
tool, not a CI gate any task pack depends on. It writes `<out-dir>/report.md`
(the exact fragment meant for a PR body) and `<out-dir>/report.json` (raw
numbers). Running it against real trials spends real LLM budget across
≥100 trials from ≥4 task families and is not part of any implementer
stage or CI job: the executed `report.md` is attached directly to the
consolidation PR's body once run — no κ or cost numbers are claimed in
this document.
