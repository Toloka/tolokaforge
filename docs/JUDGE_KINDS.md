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
attempted, so #1569 can persist them and offline replay can retry only
the failing chunk. There is never a silent partial-rubric score.

Chunk boundaries are emitted on the in-memory `JudgeResult` today via
`chunk_boundaries: tuple[tuple[str, ...], ...]` — one inner tuple per
chunk, with criterion ids in original order. Bundle-manifest persistence
is deferred to #1569.

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
