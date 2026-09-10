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

Two kinds ship in the reference distribution: `single_shot_rubric`
(wraps `LLMJudge` in one shot, byte-identical with the pre-seam
`LLMJudgeRubricEvaluator`) and `chunked_rubric` (one `LLMJudge`
invocation per fixed-K chunk of the rubric's criteria — the opt-in kind
for large rubrics where a single `submit_report` payload would exceed
the judge model's output-token ceiling). Downstream packages register
alternatives (agentic, jury) alongside without a framework PR.

> This document currently covers the parity gate and the chunked kind;
> a wider catalog is TODO (#1572).

## Chunked kind

`chunked_rubric` splits the rubric's criteria into fixed-K contiguous
chunks (`rubric.criteria[i*K:(i+1)*K]`), runs one `LLMJudge` per chunk
against a scoped sub-rubric (each chunk sees the original `reference`
verbatim), and merges the per-chunk `CriterionResult` maps into the
original full rubric — folded through `aggregate_rubric` on the
original rubric so `score` / `binary_pass` / `gate_failed` come out of
the same math the single-shot kind uses. Opt in via
`grading.llm_judge.judge_kind: chunked_rubric`; the default remains
`single_shot_rubric`.

`kind_config` schema: `{"chunk_size": int}`. `chunk_size` must be `>= 1`
(a `chunk_size >= len(criteria)` degenerates to a single call, which is
deliberate). Missing key or a `None` config → `DEFAULT_CHUNK_SIZE = 5`;
follow-up [#1581](https://github.com/Toloka/tolokaforge/issues/1581)
tunes this default from live measurement. Any unknown key or a
non-positive `chunk_size` raises `ValueError` inside `evaluate` before
any judge call runs.

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
The bundle's `grading_config.json` records `kind_config.chunk_size` from
which the chunked kind re-derives the same boundaries deterministically
on regrade.

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

`pytest --live-parity` is a flag-parity contract only today: passing
the flag opts into live-mode, and the runner then requires
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the environment. The
cassette-refresh writeback against a real `LiteLLMJudgeModelProvider`
is TODO (#1572); with the flag set the lane skips with a message
naming the missing writeback. CI never passes `--live-parity`.

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
single turn → the turn's single tool call).

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
