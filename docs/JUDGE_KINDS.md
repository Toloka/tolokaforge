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

Three kinds ship in the reference distribution: `single_shot_rubric`
(wraps `LLMJudge` in one shot, byte-identical with the pre-seam
`LLMJudgeRubricEvaluator`), `chunked_rubric` (one `LLMJudge`
invocation per fixed-K chunk of the rubric's criteria — the opt-in kind
for large rubrics where a single `submit_report` payload would exceed
the judge model's output-token ceiling), and `agentic_rubric` (a
draft → critique → submit episode over a directly-constructed
`ToolCallingLoop` — the opt-in kind for rubrics that benefit from a
self-critique pass before the final verdict). Downstream packages
register further alternatives (e.g. jury) alongside without a
framework PR.

The agentic-judge and registry decision is recorded in
[`docs/adr/0046-agentic-llm-judge-and-judgekind-registry.md`](adr/0046-agentic-llm-judge-and-judgekind-registry.md).

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
The shipping pattern (`chunked_rubric`, `agentic_rubric`) is a
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
malfunction — a malformed `submit_report`/`draft_report` past its retry
budget, turn or wall-clock budget exhaustion, or any loop-terminal
exception — surfaces as `JudgeStatus.ERRORED` with `score=None` and
`criterion_results=()`, never a `0.0`/`0.5` fallback and never a partial
verdict for a subset of criteria. Every shipped kind reuses the same
`parse_submit_report` / `aggregate_rubric` validation, so this contract
holds identically across kinds — see § Fail-loud contract under
§ Agentic kind and the fail-loud paragraphs under § Chunked kind for the
two non-trivial cases (a bad chunk, an exhausted episode).

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
   file, in the shape of § Chunked kind / § Agentic kind (config schema,
   fail-loud behaviour, any state machine or persistence notes), and a
   short entry under § Worked examples.

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

### `agentic_rubric`

```yaml
grading:
  llm_judge:
    judge_kind: agentic_rubric
    kind_config:
      critique_turn_budget: 3
      enable_critique_tool: true
```

Runs a draft → critique → submit episode over the same `ToolCallingLoop`
machinery every other kind uses, with a wider turn/timeout budget (50
turns / 480s) than `LLMJudge`'s own defaults to cover the extra phases.
Opt in for rubrics that benefit from a self-critique pass before the
final verdict — see § Agentic kind for the state machine, the critique
tool, and the `ModelCapabilities` opt-in.

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

## Agentic kind

`agentic_rubric` drives a draft → critique → submit episode over the
same `ToolCallingLoop` machinery every other kind uses, instead of the
single opinionated `submit_report` call `single_shot_rubric` and
`chunked_rubric` both make: the judge calls its read tools zero or
more times, calls `draft_report` with an initial verdict, receives one
engine-injected critique prompt echoing that draft's per-criterion
verdicts back at it, may call its read tools again, then calls
`submit_report` with its final verdict — aggregated via the same
`aggregate_rubric` every other kind uses. Both `draft_report` and
`submit_report` are registered and visible from turn 0
(`tolokaforge/core/grading/judge_kinds/agentic.py`, alongside
`judge.py`'s unconditional `submit_report` registration at
`judge.py:427`); ordering is enforced entirely by the injected
corrective messages below, not by hiding either tool per turn — dynamic
tool-list rebuilding is out of bounds for a single caller per AGENTS.md
Core Rule 7. Opt in via `grading.llm_judge.judge_kind: agentic_rubric`;
the default remains `single_shot_rubric`.

`kind_config` schema: `{"critique_turn_budget": int, "enable_critique_tool":
bool}`. Missing key or a `None` config → `DEFAULT_CRITIQUE_TURN_BUDGET = 3`
and `enable_critique_tool = True`. Any unknown key, a `critique_turn_budget
< 1`, or a non-`bool` `enable_critique_tool` raises `ValueError` inside
`evaluate`, before `judge_model_provider.build()` is ever called — the same
eager-validation contract `chunked_rubric`'s `chunk_size` follows.

### State machine

`_DraftReportTermination` is a stateful `TerminationPolicy` with two
states, `awaiting_draft` and `critiquing`, and six transitions:

1. `draft_report` while `awaiting_draft`, valid args → capture the
   draft, inject one critique `role=user` message echoing every
   captured criterion verdict plus the resolved `critique_turn_budget`,
   transition to `critiquing`, resume the loop.
2. `draft_report` while `awaiting_draft`, invalid args → reject and
   re-prompt (bounded retries, same contract as
   `SubmitReportTermination`'s `submit_report` rejection path); stay in
   `awaiting_draft`. Exhausting the retry budget yields `ERRORED`.
3. `draft_report` while already `critiquing` → inject a corrective
   message ("draft already submitted, continue critiquing, then call
   `submit_report`"); stay in `critiquing`.
4. `submit_report` while still `awaiting_draft` → inject a corrective
   message instructing `draft_report` first; no termination.
5. `submit_report` while `critiquing`, valid args → delegate to
   `SubmitReportTermination`'s validation and capture; the episode ends
   `COMPLETED`. Invalid args follow the same bounded rejection/retry
   contract as transition 2; exhaustion yields `ERRORED`.
6. No `draft_report`/`submit_report` call in the turn (pure text, or a
   read-tool call — including `critique(verdict_draft=...)`, an
   ordinary `Tool` the termination policy never matches on) in either
   state → no injected message, no state change, loop continues. This
   mirrors `SubmitReportTermination`'s own silent-continue behaviour for
   a turn without a `submit_report` call.

`critique_turn_budget` is advisory only, stated in the injected critique
message text — there is no new `TerminationReason` for exhausting it.
`AGENTIC_JUDGE_MAX_TURNS` (below) is the only hard backstop for a judge
that never progresses past the critique phase.

### Turn and timeout budget

`AGENTIC_JUDGE_MAX_TURNS = 50` and `AGENTIC_JUDGE_EPISODE_TIMEOUT_S =
480` are hardcoded kind constants, not `kind_config` fields — wider
than `LLMJudge`'s own defaults (`DEFAULT_JUDGE_MAX_TURNS = 14`,
`DEFAULT_JUDGE_EPISODE_TIMEOUT_S = 240`, `judge.py`) because one
agentic episode spans three phases (pre-draft reads, critique-phase
reads, final submit) against the same rubric, where a single-shot judge
spans one. `ToolCallingLoop.run` resets its own `max_turns` on every
call, but one agentic episode may call `.run` more than once (once per
draft/critique/submit pause); the kind enforces the 50-turn budget
*across* those resumptions by counting assistant turns already recorded
in the episode's `messages` and shrinking each resumption's
`LoopConfig.max_turns` by that count, so the effective backstop holds
for the whole episode rather than resetting on every pause. The
episode's wall-clock start time is likewise captured once before the
first `.run()` call and threaded through every resumption, so
`episode_timeout_s` also holds across the whole episode.

### `ModelCapabilities` opt-in

The kind automatically opts the judge model into `ModelCapabilities`
retry and summarization behaviour — there is no `kind_config` flag for
this, unlike every other tunable on this kind. `evaluate()` reads
`judge_model.capabilities` off the built `JudgeModel` and threads all
six of `empty_retry_count`, `output_length_retry_count`,
`parser_error_retry_count`, `tool_output_max_chars`,
`max_context_tokens`, and `context_watermark` into its own
`LoopConfig`, mirroring `tolokaforge/core/runner.py`'s
capability-threading pattern for agent loops — a first for any judge
kind (`single_shot_rubric` and `chunked_rubric` both delegate to
`LLMJudge`, which does not thread capabilities). When both
`max_context_tokens` and `context_watermark` are set on the model's
capabilities, the kind also constructs an `LLMSummarizer` and threads it
into the loop as `summarize_policy`. A `JudgeModelProvider` whose built
client lacks `.capabilities` raises `AttributeError` under this kind —
see the `JudgeModel` Protocol note in
[`docs/GRADER_SERVICE.md`](GRADER_SERVICE.md#sub-component-plug-in-seams).

### Fail-loud contract

`parse_submit_report` and `aggregate_rubric` — reused unchanged from
the other two kinds — validate every `draft_report`/`submit_report`
payload the same way `single_shot_rubric` and `chunked_rubric` do,
raising on a missing criterion verdict, an unknown/extra criterion id,
a wrong field type, or a verdict/justification-marker mismatch. Retry
exhaustion on either call (invalid args past the bounded retry count)
yields a whole-trial `JudgeResult` with `status=ERRORED`, `score=None`,
`criterion_results=()`, and a `reasons` naming the exhausted call and
the underlying validation error. A judge that ends its episode without
ever calling `submit_report` (turn budget or wall-clock exhausted
first) errors the same way. There is never a silent partial or
fabricated verdict.

### Critique tool

Alongside the injected critique prompt, the judge may call
`critique(verdict_draft: JudgeDraft) -> CritiqueContext`
(`tolokaforge/core/grading/judge_kinds/critique.py`) — an ordinary,
read-only `Tool` registered only for `agentic_rubric`, gated on
`kind_config.enable_critique_tool` (default `True`). Its schema wraps
the exact `submit_report`/`draft_report` argument shape inside a single
top-level `verdict_draft` object, so a flat, `submit_report`-shaped
call is rejected by `ToolExecutor`'s `jsonschema` validation before
`critique.execute` ever runs; `execute` re-validates with
`parse_submit_report`, inheriting every one of `submit_report`'s
fail-loud checks (missing verdict, unknown criterion id, wrong type,
verdict/justification-marker mismatch) for free.

`critique` never gathers new information — it deterministically
resolves evidence the episode already collected, case-insensitive
substring-matching each criterion's description/expected tokens
against three sources: the formatted transcript, the state diff's
lines when one was recorded, and a *replay* of any prior `search_kb`
results already present in the judge's own in-progress transcript (no
new `KnowledgeSearch` call). A criterion with no matching text
contributes zero pointers, never a fabricated placeholder. Because
`critique` is an ordinary `Tool` the termination policy never matches
on, calling it exercises transition 6 above like any other read-tool
call — no state-machine change was needed to add it.

Registration happens inside `agentic.py`'s own `_build_episode_setup`,
strictly before that function's `tool_schemas` snapshot — the only call
site `AgenticRubricJudgeKind.evaluate` uses. `single_shot_rubric` and
`chunked_rubric` both go through `LLMJudge.run` -> `build_judge_registry`
directly and never execute `_build_episode_setup`, so `critique` cannot
leak into either kind's registry by construction.

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
single turn → the turn's single tool call). See the `agentic_rubric`
cassettes (any fixture under `tests/data/judge_kind_parity_corpus/`) for
the single-client, multi-turn authoring shape instead — one script with
two turns (`draft_report`, then `submit_report`) under
`judge_scripts.agentic_rubric`.

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

Every fixture MUST also ship a `judge_scripts.agentic_rubric` block of
exactly two turns — a `draft_report` call, then a `submit_report`
call (the critique prompt between them is engine-injected, not a
scripted turn) — with the `submit_report` turn's per-criterion verdicts
matching the fixture's `single_shot_rubric` cassette for identical
cross-kind κ.

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
  --kinds single_shot_rubric,chunked_rubric,agentic_rubric \
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
