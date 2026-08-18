# Judge Replay — offline re-judging of a recorded run

`tolokaforge rejudge` re-executes **only** the rubric-judge stage over a recorded
run, producing new replay grade artifacts and leaving the originals untouched. It
does not re-run the agent or any environment service, so it spends judge tokens
only.

Re-checking the **deterministic** side of the same recorded run — a pack's
[`trace_checks`](GRADING.md#trace-checks) constraints — is
[`tolokaforge retrace`](TRACE_REPLAY.md), a separate command because it spends
nothing at all: no judge, no tokens, no containers. Reach for `rejudge` when the
judge is what changed, and for `retrace` when a constraint is. Neither command
discovers the other's output.

Deciding whether a constraint can *replace* a rubric criterion is a third command,
[`tolokaforge reconcile`](RUBRIC_MIGRATION.md), which also spends nothing: it reads the
judge verdicts a bundle already recorded rather than producing new ones. Its labels come
only from the original bundle's `grade.yaml.criterion_results` and never from a replay
grade — a replay's deterministic components and gates are absent (#775), so a replay grade
cannot supply the label a migration is judged against.

## When to use it

- **Validate a judge change.** After editing the `submit_report` schema, the judge
  prompt, or rubric wording, re-judge a recorded run and compare verdicts instead
  of paying for a full agent re-run (which also confounds agent variance with
  judge variance).
- **Compare judge models.** Re-judge the same trajectories under a different
  `--judge-model` to A/B two judges on identical evidence.
- **Post-hoc audit.** Re-derive per-criterion verdicts for a run whose grades are
  under question, with the judge's transcript recorded for inspection.

Calibration against hand-authored golden labels is a different tool
(`tools/rubric-calibrator`); replay re-judges a *recorded run*, not a fixture set.

## Usage

```bash
# Preview: discover + classify + resolve inputs, spend nothing.
uv run tolokaforge rejudge --source <run-or-bundle-dir> --dry-run

# Re-judge with the recorded rubric + judge model.
uv run tolokaforge rejudge --source <run-dir>

# Re-judge with a different judge model / an overridden rubric.
uv run tolokaforge rejudge --source <run-dir> \
    --judge-model openrouter/openai/gpt-4.1-mini \
    --grading path/to/grading.yaml
```

| Flag | Meaning |
|---|---|
| `--source` | A run dir (`trials/<task>/<idx>/` subtree), a flat collection of bundle dirs, or a single bundle dir. A directory is a trial bundle iff it directly contains `trajectory.yaml` — the one file every writer produces — so a trial that produced no grade is discovered and reported as a no-grade skip. |
| `--trial` | Re-judge a single bundle dir instead of the whole `--source`. |
| `--judge-model` | Override the judge model as `<provider>/<model>` (e.g. `openrouter/openai/gpt-4.1-mini`), temperature 0. Default: the recorded `model_config.judge`. |
| `--grading` | Override the rubric — and, when the file carries them, the judge's custom prompt (`llm_judge.customization.system_prompt`) and agent-policy gating (`llm_judge.customization.include_agent_system_prompt`) — with a supplied `grading.yaml` (or a bare `rubric:` mapping). Required for old bundles that recorded no rubric. Default: the recorded rubric, prompt, and gating. |
| `--knowledge-search` | `recorded` (honour the bundle's recorded gating), `on`, or `off`. Default: `recorded`. Forcing `on` for a bundle with no recorded KB gating cannot conjure a KB tool: the replay grade records `offered: []` and the provenance records the mode — observable, not silent. |
| `--replay-id` | Name for the artifact subdirectory. Default: a timestamped id. |
| `--dry-run` | Discover, classify, and resolve inputs, then report what would replay — spending nothing. The census it prints (`<N> discovered: <e> eligible, <s> not-applicable, <g> no-grade, <f> failed`) is the same one a real batch prints. |

`rejudge --judge-model` takes a full `<provider>/<model>` ref — the first path
segment selects the provider — unlike `run --judge-model`, which takes a bare
model name and always routes through OpenRouter.

Execution is **sequential with no concurrency cap**; there is no automatic
cost ceiling. Use `--dry-run` to inspect the eligible trial count and the resolved
judge model before spending. API keys are resolved through `SecretManager`.

## What gets re-judged

**A directory records a trial iff it directly contains `trajectory.yaml`**, and every
recorded trial is discovered. One file, because it is the only one every writer
produces — a trial that never ran carries no `task.yaml` and one nothing graded
carries no `grade.yaml`, so keying identity on either removes recorded trials from
the batch instead of reporting what they are. The rule lives in
`tolokaforge/core/grading/replay_layout.py` and all three offline commands discover
through it, so a directory is a bundle for `rejudge` exactly when it is one for
[`retrace`](TRACE_REPLAY.md) and [`reconcile`](RUBRIC_MIGRATION.md).

**`replays` and `trace_replay` are reserved directory names.** Nothing beneath either,
at any depth under `--source`, is discovered: `replays/` is this command's own output
and `trace_replay/` is retrace's, so a re-run never re-judges its own artifacts and
neither command reads the other's. The deliberate cost is that a *task* named
`replays` would hide its own trials.

Each discovered trial is classified:

- **Judge-eligible** — the recorded `grade.yaml` carried a judge stage
  (`judge_status` is `completed` or `errored`). These are re-judged.
- **Not-applicable** — the trial never had a judge stage (`judge_status:
  unspecified`, a state/transcript-only trial). These are **skipped and never
  judged**, even when `--grading` supplies a rubric — a rubric override never
  conjures a judge stage onto a trial that never had one (that would spend tokens
  on a task that was never rubric-graded).
- **No-grade** — the bundle carries no `grade.yaml` at all, so there is no judge
  stage to replay. Also **skipped and never judged**, and classified before any
  input is reconstructed, so such a bundle resolves no rubric and no judge model
  and cannot spend. The skip's reason names which grade-less shape it is, read
  from the bundle's own `trajectory.yaml` through the same outcome classification
  the run uses: a trial grading refused (`grading_error` recorded — see
  [`OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) § `trajectory.yaml`), a trial the
  infrastructure aborted before it was measured (`termination_reason` — see
  [`GRADING.md`](GRADING.md#infrastructure-aborts-produce-no-grade)), or —
  reported as the anomaly it is — a grade-less trial that is neither. The
  reason names the outcome **class**; for a provisioning
  failure, *why* the environment did not come up is `error_reason` in the same
  bundle's `metrics.yaml`.

A judge-eligible trial that cannot be reconstructed (a judge ran, but the bundle
records no rubric and no `--grading` was given; or no transcript; or no
`prompts.yaml` agent policy; or no judge model and no `--judge-model`) is reported
as a **named per-trial failure** — the batch continues, and no recorded trial under
`--source` is ever silently skipped. The same applies to a bundle whose `grade.yaml` is present
but unreadable (it cannot be classified), to a bundle with no grade whose
`trajectory.yaml` is absent or unreadable (it cannot say why it has none), and to
recorded inputs that fail validation (a corrupt `trajectory.yaml`, rubric, or
model config). When any trial
fails, `rejudge` still writes the comparison report for the replayed subset and
then **exits non-zero**, so a scripted caller never reads a partially-failed
replay as clean. A skip never does: neither a not-applicable trial nor a no-grade
one moves the exit code, because neither is a defect. **A `--source` that
discovers no bundle at all exits non-zero naming the directory searched** — a
batch that claims nothing and returns success is the same silence at full scale.

**The batch's size is legible.** A run that hit provider throttling has the same
number of recorded trials as the same suite on a clean run; what differs is how
many of them were judge-eligible, and the no-grade skips account for the rest by
name. Two batches over one suite are therefore comparable without reading a
second file.

**Two entry points, one answer.** A bundle named with `--trial` gets the
disposition discovery would have given it, because both paths reach the same
classification.

## Offline read tools

The live run's judge could read the database, a knowledge base, or the agent's
workspace. Replay has no live services, so those read tools are offered to match
the recorded surface but backed by **offline shims** that return an explicit
`unavailable in replay: <backend>` marker — never a silent empty result. The
judge therefore grades knowing what it could not inspect, and the marker is
visible in the replay's `judge_trajectory.yaml`. Reconstructing real recorded
state so the offline judge can inspect it is tracked separately (issue #525).

## Custom judge system prompt

If the recorded run's judge used a custom system prompt
(`grading.llm_judge.customization.system_prompt`), replay reconstructs it from the
bundle's `task.yaml` — the same source as the rubric — and re-runs the judge with
it (the marker contract is always appended, so the reconstructed prompt still
validates `submit_report`). An old bundle with no recorded customization replays
with the default prompt (the declared fallback).

The rubric and the custom prompt resolve **independently**. A `--grading` override
replaces the prompt **only when its own `llm_judge.customization.system_prompt` is
set**; a rubric-only override leaves the recorded prompt in effect and never
resets it to the default. `replay_provenance.yaml` stamps both
`custom_system_prompt` (whether one was in effect) and `custom_prompt_source`
(`recorded` / `override`), so a rubric-only override over a custom-prompted bundle
reads `rubric_source: override` while `custom_prompt_source: recorded`.

## Agent-policy evidence gating

If the recorded run gated the agent's policy out of the judge's evidence
(`grading.llm_judge.customization.include_agent_system_prompt: false`), replay
reconstructs the gating from the bundle's `task.yaml` — the same source as the
rubric — and re-runs the judge with it, so the replayed opening message withholds
the agent policy exactly as the recorded run did. An old bundle with no recorded
value replays with the agent policy **included** (the declared fallback: old runs
graded with the policy present).

The gating resolves **independently** of the rubric, like the custom prompt. A
`--grading` override flips it **only when its own
`llm_judge.customization.include_agent_system_prompt` is set**; a rubric-only
override leaves the recorded gating in effect. `replay_provenance.yaml` stamps
`include_agent_system_prompt` (the effective decision) and `agent_prompt_source`
(`recorded` / `override`, or `null` when the gating defaulted to include), so a
rubric-only override over a gated bundle reads `rubric_source: override` while
`agent_prompt_source: recorded`.

## New-vs-old bundle replayability

- **New bundles** (recorded with a `judge_inputs.yaml`) replay at **full
  fidelity**: the judge's opening message is rebuilt from the exact recorded
  `state_diff` string, so the reconstruction matches what the live judge saw.
- **Old bundles** (predating the structured inputs) replay in a declared
  **fallback**: the opening message omits the `state_diff` (it was never
  persisted structurally), so a `state_diff`-influenced verdict may not reproduce.
  The fallback is stamped in `replay_provenance.yaml` (`fidelity_mode: fallback`),
  never applied silently.
- **Redacted bundles do not replay at all.** A bundle whose `metrics.yaml` carries a
  `redaction` stamp is refused by name before any judge spend: the transcript it
  would rebuild carries argument values a policy rewrote, so the judge would be
  shown — and would grade — evidence the agent never produced.

## Output

Replay artifacts are written under `<source>/replays/<replay_id>/`, mirroring the
discovered bundle path. **Originals are never opened for write.** Per replayed
trial:

- `grade.yaml`, `judge_trajectory.yaml`, `judge_inputs.yaml` — the same formats as
  a normal trial bundle (so a replay bundle is itself replayable).
- `replay_provenance.yaml` — the judge model used, whether each of the judge
  model / rubric / KB-gating / custom prompt came from the bundle or an override,
  and the fidelity mode.

Under a redacting artifact-write policy both judge sidecars are withheld and a
`metrics.yaml` appears carrying the redaction stamp alone — the writer's
declaration that they were withheld rather than never produced (see
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md:1) § `redaction`), which is what makes
the replay bundle refusable in turn.

The batch also writes one `replays/<replay_id>/replay_report.yaml` — the per-run
comparison against the recorded originals. A batch that replayed **nothing**
writes no report: there is no comparison to make, and the console carries that
batch's census instead.

## Reading the comparison report

`replay_report.yaml` (and its console summary) reports:

- **The batch census** (`batch`) — `discovered` and, summing to it, `replayed`,
  `skipped_not_applicable`, `skipped_no_grade` and `failed`. `trials` below covers
  the *compared* subset; the census covers the whole source, which is what makes
  two batches over one suite comparable: "fewer trials were judge-eligible" is a
  different fact from "the provider throttled us", and only the census tells them
  apart. A census whose parts do not sum to `discovered` is refused rather than
  written.
- **Per-criterion** `original` vs `replay` `met`/`score` per trial, with the
  per-criterion `met_agrees` and `score_delta`.
- **Agreement rate** — the fraction of criteria whose `met` matches, computed over
  **`comparable`** trials only (both sides produced per-criterion verdicts).
- **Aggregate `llm_judge` delta** — mean replay score minus mean recorded
  `llm_judge` component, over comparable trials. Non-judge components (state
  checks, transcript rules) are **carried** from the recorded grade, not
  recomputed — the report says so.
- **Judge-only usage + cost** — summed across the replayed trials.

Trials are bucketed, and only `comparable` trials count toward the agreement rate:

| Bucket | Meaning |
|---|---|
| `comparable` | Both the recorded and the replay judge produced verdicts — counted. |
| `original_errored` | The recorded judge errored — nothing to diff; replay side listed, not counted. |
| `original_no_verdict` | The recorded judge produced no criteria — nothing to diff; not counted. |
| `replay_errored` | The replay judge errored — its own bucket, never a fabricated `0`. |

The judge loop is agentic and not bit-reproducible even at temperature 0, so
reproduction is a **verdict-level** expectation on unambiguous criteria, not a
byte-level one. See [`docs/RUBRIC_GRADING_DESIGN.md`](RUBRIC_GRADING_DESIGN.md) and
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md).
