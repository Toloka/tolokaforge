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

Three user-facing kinds ship in the reference distribution:
`single_shot_rubric` (wraps `LLMJudge` in one shot, byte-identical with
the direct `LLMJudgeRubricEvaluator` path), `multi_turn_rubric` (a
baked-in composition that samples the judge K=3 times with
geometric-median aggregation over auto-anchored rubrics — see
§ Choosing a kind), and `auto_rubric` (inspects the rubric shape and
routes deterministically between the two above — see § Choosing a kind).
Two additional kinds — `voted_rubric` and `auto_anchored_rubric` —
ship as internal building blocks (§ Internal building blocks); user
task configs should not select them directly. Downstream packages
register further alternatives (e.g. agentic) alongside without a
framework PR.

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
A kind that takes no options (`single_shot_rubric`) still receives
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
kinds.

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
   `tests/data/judge_kind_parity_corpus/` needs a
   `judge_scripts.<your_kind_name>` cassette. See § Corpus authoring
   rules for the per-fixture cassette shape.
4. **Clear the parity gate.** Run the canonical parity lane
   (`tests/canonical/test_judge_kind_parity.py`) with your kind named in
   its parametrise list. Every criterion must land `pass` or `warn` under
   both cross-kind agreement (vs. `single_shot_rubric`) and
   self-consistency (§ Three-level thresholds) before the kind is
   default-eligible for any task pack.
5. **Document the kind.** Add a `## <Your kind> kind` section to this
   file, in the shape of § Voted kind (config schema, fail-loud
   behaviour), and a short entry under § Worked examples.

## Choosing a kind

Three user-facing choices, one line each:

- **`single_shot_rubric`** — one judge call per grade, deterministic,
  cheap. Recommended when every graded criterion in your rubric carries
  an author-written `expected:` anchor.
- **`multi_turn_rubric`** — 3-sample judge with auto-generated anchors
  and geometric-median aggregation. Roughly 4× the cost of `single_shot`
  (1 anchor warm-up + 3 grading calls) and reduces grading variance on
  rubrics with unanchored graded criteria.
- **`auto_rubric`** — inspects the rubric shape at grade time and picks
  between the two above deterministically. Recommended default: routes
  to `single_shot_rubric` when every graded criterion is anchored and to
  `multi_turn_rubric` when any graded criterion has no `expected:`
  anchor.

There are no user-tunable knobs on any of these three kinds. The
composition inside `multi_turn_rubric` and the selection rule inside
`auto_rubric` are baked in — passing a non-empty `kind_config` to
either raises `ValueError` at grade time.

## Worked examples

One minimal `grading.llm_judge` snippet per user-facing kind.

### `single_shot_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: single_shot_rubric
```

No `kind_config` — the kind receives it on the Protocol and discards it
unread. One `LLMJudge` call produces the whole rubric's verdict in a
single `submit_report`; this is the default, byte-identical with the
direct `LLMJudgeRubricEvaluator` path, and every task pack with no
`judge_kind` field runs this kind unchanged.

### `multi_turn_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: multi_turn_rubric
```

No `kind_config`. Runs the baked-in composition
`voted(n=3, geometric_median) → auto_anchored → single_shot`: one
anchor warm-up call synthesises `expected:` anchors for unanchored
graded criteria, then three grading samples are aggregated by
geometric-median voting. Total per grade: 1 warm-up + 3 grading calls.

### `auto_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: auto_rubric
```

No `kind_config`. Inspects the rubric shape at grade time: dispatches
`multi_turn_rubric` when any graded criterion has `expected: None`,
otherwise dispatches `single_shot_rubric`. The selection appears as a
one-line prefix on `JudgeResult.reasons` so the audit trail shows which
mode graded a trial.

## Internal building blocks

`voted_rubric` and `auto_anchored_rubric` remain registered in the
`tolokaforge.judge_kinds` entry-point group and importable from
`tolokaforge.core.grading.judge_kinds`. They exist so `multi_turn_rubric`
can compose them and so downstream packages that want alternative
compositions have building blocks to reach for. User task configs
should not select them via `judge_kind:` — use `single_shot_rubric`,
`multi_turn_rubric`, or `auto_rubric` instead.

## Voted kind (internal building block)

Selecting `voted_rubric` via a task's `judge_kind:` is not a user-facing
choice — use `multi_turn_rubric` instead. This section documents the
building block for downstream package authors that need to compose
alternative stacks.

`voted_rubric` wraps any other registered `JudgeKind` (default
`single_shot_rubric`), calls its `evaluate` `n_samples` times (default
3) against the SAME rubric evidence — the wrapped kind always receives
`kind_config=None`, so a nested config on the wrapped kind is not
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

Per-sample fail-loud: any sample whose `JudgeResult.status` is not
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

Cost note: K samples consume up to `K ×` the wrapped kind's per-trial
wall-clock, tokens, and cost. This is the acknowledged cost of reducing
judge-model self-variance.

## Auto-anchored kind (internal building block)

Selecting `auto_anchored_rubric` via a task's `judge_kind:` is not a
user-facing choice — use `multi_turn_rubric` or `auto_rubric` instead.
This section documents the building block for downstream package
authors that need to compose alternative stacks.

`auto_anchored_rubric` is a wrapper kind that closes the "fuzzy criterion
wording" drift class without pushing work onto rubric authors. Before
delegating to its wrapped kind (default `single_shot_rubric`, configurable
via `kind_config.wrapped_kind`), it runs a **single warm-up judge call per
unique (rubric, judge_model) tuple** that asks the judge to produce a
one-sentence anchor for each `kind: graded` criterion the author left
with `expected: None`. Those anchors are cached in-process, folded into a
synthetic `Rubric` (author-written `expected:` anchors pass through
unchanged), and the wrapped kind is dispatched with that anchored rubric.

Motivation: a subjective criterion whose description alone
under-specifies "met" is scored against a different anchor on each
sample. `auto_anchored_rubric` commits every trial in the flight to ONE
shared anchor so subsequent grades score against the same standard.

Downstream compositions can instantiate this kind directly (or via
`load_judge_kind("auto_anchored_rubric")`) with a `kind_config` naming
the wrapped kind:

```yaml
kind_config:
  wrapped_kind: single_shot_rubric
```

The user-facing `multi_turn_rubric` bakes the `voted → auto_anchored →
single_shot` composition in with no user knobs — empirical A/B evals
identified it as the stack that closes measured drift classes without
unnecessary complexity, and users who want that composition should
select `multi_turn_rubric` rather than composing it by hand.

Fail-loud contract: any warm-up call that returns non-JSON, is missing an
anchor for one of the unanchored criterion ids, or hands back a
non-string / empty anchor raises `PerRubricAnchorGeneratorError` before
any wrapped-kind dispatch. Author-written anchors pass through unchanged
so a mixed rubric (some anchored, some auto-anchored) grades against a
consistent mix of author and judge-authored `expected:` fields.

Audit trail: every auto-anchor lands in `JudgeResult.reasons` prefixed
with `auto_anchored_rubric warm-up anchors:` so a reader can see what
the harness told the judge "met" looks like. The auto-anchor is written
verbatim into each criterion's `expected:` in the wrapped-kind's dispatch
with no synthetic tag — the wrapped judge sees an anchor indistinguishable
from an author-written one, so it cannot bias on provenance.

Cost: **+1 judge call per unique (rubric, judge_model)** — amortised
across every trial in the process that uses the same rubric + judge.
On a 50-trial flight sharing one rubric that is 1 extra call spread over
50 grades: effectively free. The cache is in-process only (no disk
persistence at v1); a follow-up may add per-rubric anchor persistence.

## Grade-injection defenses

Model-controlled slots (`agent_system_prompt`, each transcript
message's `content`, tool-call `arguments`) are neutralised at
interpolation time before they land in the judge prompt. The exact
fence strings the judge prompt uses to fence untrusted evidence
(`===== TRANSCRIPT =====`, `===== END TRANSCRIPT =====`) are replaced
with a spaced/zero-width-space variant if they appear inside those
slots, so a payload that replays the fence cannot break out of the
transcript block and inject fake instructions into what the judge
reads as harness text. Free-form Markdown (`---`, triple-backtick) is
NOT neutralised — those are common in honest agent output and blanket-
neutralising them would garble every trial. The defense is a
byte-level string replacement in
[`tolokaforge/core/grading/judge.py::_neutralise_judge_delimiters`](../tolokaforge/core/grading/judge.py);
honest trials see no change (the fence strings never appear in normal
output), and payloads that carry the fence keep their content in the
prompt but lose the anchor.

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
(partial completion, `status == ERRORED`) contributes NO paired
observation and lands in the report's `errored_fixture_ids` with a
reason naming the entry_id, the leg that fell short, and the missing
criterion ids. A candidate that emits verdicts for 4 out of 5 criteria
is not "80 % agreeing" — it is failing to grade one criterion, and the
gate reports it as such.

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
`judge_scripts[kind_name]` — every other key in the fixture file is
preserved byte-identical. This is the cassette-refresh mechanism: it
keeps the 20-fixture corpus in sync with what a shipped kind's real
judge model actually says today, so the cassette-mode lane above keeps
replaying a faithful script. CI never passes `--live-parity` —
refreshing cassettes is a manual step run against real budget when a
kind's prompt or behaviour changes.

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
include_agent_system_prompt, judge_scripts}`. The
`judge_scripts.<kind_name>` value is a list of turns; each turn is
either a string (assistant text) or a list of tool-call dicts
(`{name, arguments}`). The `single_shot_rubric` cassette is one turn
calling `submit_report` with per-criterion verdict + justification
args; the justification MUST use YAML double-quoted syntax so `\n`
is interpreted as a real newline (the judge's verdict-consistency
regex needs the marker on its own line).

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
  --kinds single_shot_rubric,multi_turn_rubric \
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
