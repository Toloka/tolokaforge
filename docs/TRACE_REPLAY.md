# Trace Replay — re-checking trace constraints over a recorded run

`tolokaforge retrace` rebuilds each recorded trial's
[event timeline](GRADING.md#trial-event-timeline) from its bundle and scores a
[`trace_checks`](GRADING.md#trace-checks) block against it again. No agent, no
environment service, no judge — so it starts no container and spends no tokens. The
guarantee is structural: `tolokaforge/core/grading/trace_replay.py` reaches the one
production evaluator, the bundle reader and the authoring gate, and stops there, which
a clean-subprocess import probe holds
(`tests/canonical/test_retrace_cli.py::test_the_replay_module_reaches_neither_an_llm_client_nor_the_judge`).

`rejudge` and `retrace` are separate commands because they cost different things:
[judge replay](JUDGE_REPLAY.md) re-runs the rubric judge and spends judge tokens per
trial, `retrace` re-runs a deterministic component and spends nothing. Folding a free
operation into a paid one is how an operator pays by accident.

[`tolokaforge reconcile`](RUBRIC_MIGRATION.md) is the third command over a recorded run and
also spends nothing, but it is separate from `retrace` for a different reason: it joins each
recomputed constraint verdict to the *judge's* recorded verdict for a rubric criterion and
exits non-zero on the verdict of a decision. `retrace`'s exit code is reserved for the
readability of the corpus — a non-discriminating constraint and a disagreement with the
recorded grade both exit `0` here, deliberately — so a gate on a migration decision cannot
live in it without changing that contract.

## When to use it

- **Decide whether a constraint is worth shipping.** A constraint that passes every
  recorded trial, or fails every one, separates nothing: it adds no signal to the
  pack. `retrace` reports that per constraint over a corpus, which is the question
  the [pre-run authoring gate](GRADING.md#what-is-validated-before-a-run) cannot
  answer — the gate reads the block, not the trials.
- **Iterate on a candidate constraint without touching the pack.**
  `--constraints <file>` supplies the block, so the loop is write → `retrace` → read
  the discrimination → commit only what discriminated.
- **Audit a recorded verdict.** Each constraint's re-check is compared against the
  verdict the live run recorded *for that constraint*, so a disagreement between the
  two is visible.

## Usage

```bash
# Preview: discover, classify, rebuild every timeline, write nothing.
uv run tolokaforge retrace --source <run-or-bundle-dir> --dry-run

# Re-check each bundle against the block its own pack declared.
uv run tolokaforge retrace --source <run-dir>

# Re-check every bundle against a supplied block instead.
uv run tolokaforge retrace --source <run-dir> --constraints candidate.yaml
```

| Flag | Meaning |
|---|---|
| `--source` | A run dir (`trials/<task>/<idx>/` subtree), a flat collection of bundle dirs, or a single bundle dir. |
| `--trial` | Re-check a single bundle dir instead of the whole `--source`. |
| `--constraints` | A grading document carrying a `trace_checks:` key, or a bare `constraints:` / `alternatives:` block. **Replaces** each bundle's block wholesale — never merges. Default: the block each bundle recorded. |
| `--replay-id` | Name for the artifact subdirectory — letters, digits, `.`, `_` and `-` only, and not `.` or `..`. Default: a timestamped id. |
| `--dry-run` | Discover, classify and rebuild each timeline, then report what would be re-checked. Nothing is written, so there is no discrimination table: nothing was measured. |

A merge of two constraint lists has no auditable meaning: partial satisfaction across
declared routes is [already a grading bug](GRADING.md#alternative-paths), and two lists
folded together assert something neither was written to assert. So `--constraints`
replaces.

### Exit codes

| Outcome | Exit code |
|---|---|
| every discovered bundle was re-checked or declared-skipped | `0` |
| a constraint separated nothing (`always_true`, `always_false`, `never_decided`, `undecided_in_part`, `not_measured`) | `0` |
| a re-check disagrees with the verdict the live run recorded for that constraint | `0` |
| a bundle cannot be classified or reconstructed | `1`, after the per-bundle lines and the report |
| `--constraints` cannot be loaded, or fails the authoring gate | `1`, before any trial is re-checked; nothing is written |
| `--source` holds no bundle at all | `1`, naming the source; nothing is loaded |
| two bundles claim one task while declaring different `trace_checks` blocks | `1`, naming both; the batch ran, no report was written |
| `--replay-id` is not a single directory name | `2`, Click's usage code; nothing is loaded |

**Exit zero on a non-discriminating constraint is deliberate.** Both that and a
disagreement with the recorded grade are *results* — an author iterating on a
candidate expects to read `always_true` and keep working, and a CI job must not go
red because a corpus turned out to be uninformative. A caller that needs to gate on
them reads `trace_replay_report.yaml`, which carries every count. A gating mode would
be a new explicit flag, not a change to this default.

## What gets re-checked

A directory records a trial iff it directly contains **`trajectory.yaml`**, and every
recorded trial is a bundle. One file, because it is the only one every writer
produces: `task.yaml` comes from the conductor alone and `grade.yaml` only where a
verdict exists, so a trial that never ran or was never graded carries neither. The
rule has one home, `tolokaforge/core/grading/replay_layout.py`, and all three offline
commands discover through it, so a directory is a bundle for `retrace` exactly when it
is one for [`rejudge`](JUDGE_REPLAY.md) and
[`reconcile`](RUBRIC_MIGRATION.md).

The weaker filter is the deliberate cost: a directory holding a stray
`trajectory.yaml` is a bundle, and a command pointed at its parent reports it as a
named failure rather than passing over it in silence.

**`trace_replay` and `replays` are reserved directory names.** A bundle sitting beneath
a directory with either name, at any depth under `--source`, is not discovered:
`trace_replay/` is this command's own output and `replays/` is
[judge replay's](JUDGE_REPLAY.md), so neither command's discovery reads the other's
artifacts, and a source re-pointed at a run that already holds replay output re-checks
its trials rather than its results. The depth is unrestricted because a
previously-replayed subtree can be nested anywhere beneath whatever the operator points
at. The cost of that is stated rather than discovered: a *task* named `replays` or
`trace_replay` would hide its own trials from discovery, which is the trade for an
isolation that holds whatever either command writes into its tree.

`reconcile` ([RUBRIC_MIGRATION.md](RUBRIC_MIGRATION.md)) is **not** a third reserved name, and
does not need to be: its isolation comes from its *write set* rather than from discovery. It
writes exactly one file — `reconcile/<replay_id>/reconcile_report.yaml` — and never a per-bundle
marker, so nothing it leaves under `--source` can be mistaken for a bundle by anything.

Each discovered bundle gets one of five dispositions, all of them reported:

| status | meaning |
|---|---|
| `replayed` | re-checked, artifacts written |
| `would_replay` | `--dry-run`: eligible and reconstructable |
| `skipped_not_applicable` | the bundle's `grading_config` declares no `trace_checks` and no override was supplied |
| `skipped_no_task` | the bundle carries no `task.yaml` and its own trajectory records an infrastructure abort, so nothing says what the trial would have been checked against |
| `failed` | an input is missing or invalid; the reason names the file and the defect |

**A bundle with no `task.yaml` is classified rather than failed when its own
trajectory says the trial never ran.** A trial whose environment did not come up is
written by the executor alone — `trajectory.yaml` and `metrics.yaml`, because the
conductor never ran — so nothing in it says what it would have been checked against.
That is `skipped_no_task`, named from the outcome class the trial's own trajectory
records, and like every skip it never affects the exit code. The rule is deliberately
narrow: a task-less bundle whose trajectory records a real episode has lost its task
snapshot and is `failed` naming `task.yaml`, and one whose `trajectory.yaml` is
missing or unreadable is `failed` naming that file. The reason names the outcome
class; *why* the environment did not come up is `error_reason` in the same bundle's
`metrics.yaml`.

It is kept apart from `skipped_not_applicable` on purpose: that status asserts
something about the pack — it declares no `trace_checks` — which a bundle carrying no
`task.yaml` cannot support. A "cannot answer" reported as "nothing to check" is the
silent number this command exists to avoid.

A skip is declared, never silent. A failure never aborts the batch — the readable
trials are still measured and still reported — but it does make the command exit
non-zero, so a scripted caller never reads a partially-failed replay as a clean one.
That holds down to the bytes: every file a bundle carries is read through one reader,
so a file the filesystem refuses or one holding anything but UTF-8 is `failed` naming
that file, not a decoding traceback out of the batch.

**A bundle predating call-id threading cannot be re-checked at all**, and says so: the
call id is the only key joining a tool call to the result it produced, so a bundle
that persisted calls without one is `failed` with that reason rather than a pydantic
traceback. The run-level evidence counts those separately from other unreadable
inputs, because it is a property of the corpus's age rather than of a broken file.

**The bundle schema stamp is evidence, never a gate.** A stamp says which artifacts to
expect; both stamped and unstamped bundles re-check fine, so nothing rejects a bundle
for its version. See [`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md).

### A supplied block is checked before anything is replayed

`--constraints` is run through the same
[`inspect_grading_authoring`](GRADING.md#what-is-validated-before-a-run) a pack meets
before a run, against the tool set each bundle *recorded* (`tools_schemas.yaml` — the
post-policy list the provider saw). An **error** aborts the batch naming the file and
the defect: a misspelled tool name is one defect in one file, and replayed it would
arrive as a corpus of trials that all failed a constraint selecting nothing.
Advisories and rules the gate could not answer are reported and the batch continues.

A bundle that recorded no `tools_schemas.yaml` has an **unresolvable** tool set, not
an empty one — every schema-dependent rule for it lands in the `unchecked` channel and
the console says so per bundle and once in full. A gate that could not run must never
read as a clean bill of health.

## Evidence and undecided verdicts

A constraint verdict is `undecided` when the trial does not carry the evidence to
decide it — see [GRADING.md § When a constraint cannot be decided](GRADING.md#when-a-constraint-cannot-be-decided).
Over a recorded corpus the usual cause is a bundle carrying no `tool_log.yaml`: every
`status` and `executor` predicate is unreadable there, every `result` on an unanswered
call with it, and so is any binder reading one.

So a discrimination verdict is only as good as the corpus behind it, and the report
carries a run-level `evidence` block saying what the corpus was: how many bundles were
read, how many carried a tool-call record, how many were skipped, how many carried no
task snapshot, how many failed, how many were rejected as pre-call-id, and which schema
stamps were seen (`unstamped` included). The task-less count is its own number rather
than part of `bundles_skipped`: what an aborted trial could not say about a pack and
what a pack chose not to declare are two facts, and one number carrying both is a
number nobody can act on. An operator reading `never_decided` needs to know whether the corpus is old
before concluding anything about the constraint.

`tool_log_present` is the reader's file-presence answer, not the timeline's
`records_present`: a trial that called no tool writes `tool_log.yaml` empty, so its
bundle is fully recorded while the timeline it produces reports no record view.
Reading the timeline's flag would report a fully-recorded corpus as record-less.

## Reading the discrimination report

One row per constraint the resolved block **declares**, keyed by `(task_id,
constraint_id)`. The task id is half the key because ids are unique only within one
pack's block, and a run dir spans tasks: keyed on the id alone, two packs that both
name a constraint `no_failed_calls` would fold into one row mixing two predicates over
two corpora.

| verdict | condition |
|---|---|
| `discriminating` | ≥ 1 decided trial passed **and** ≥ 1 decided trial failed |
| `always_true` | **every evaluated trial was decided**, and all passed |
| `always_false` | **every evaluated trial was decided**, and all failed |
| `undecided_in_part` | ≥ 1 decided and ≥ 1 undecided, the decided trials agreeing; carries `decided_verdict` |
| `never_decided` | ≥ 1 trial evaluated, none decided |
| `not_measured` | the constraint was evaluated on no trial |

The boundaries are **all decided / some decided / none decided** — no threshold
anywhere. `always_true` and `always_false` therefore mean unanimous *on complete
evidence*, which is a strictly stronger claim than "nothing contradicted it", and
everything short of it is named: a constraint undecided on two trials and decidably
false on one is `undecided_in_part` with `decided_verdict: false`, not a corpus-wide
condemnation resting on a single observation. Disagreement wins outright: a constraint
that passed one trial and failed another is `discriminating` however many trials were
undecided, because it is *proven* to discriminate.

**`not_measured` is decided before any other verdict.** Over zero evaluated trials
"every evaluated trial was decided and all passed" is vacuously true and so is its
mirror, so a classifier checking the members in declaration order would report a
constraint no trial ever evaluated as unanimously passing.

**A path constraint is measured only over the trials its path won.**
`evaluate_trace_checks` emits results for the shared constraints plus the winning
route's only, so a route that lost every trial has its constraints evaluated zero
times. They still get a row — `trials_evaluated: 0`, verdict `not_measured`, `route`
naming the path — because a constraint that vanished from the report would read as one
that was never declared. Every row carries `route` (`""` for shared) beside
`trials_evaluated`, and the report states the denominator in one line, since
`always_true` on a route that won twice out of twenty otherwise reads as a corpus-wide
claim.

Undecided trials are excluded from `passed_trials` / `failed_trials` and counted in
`undecided_trials`, and `trials_evaluated` sits beside `trials_decided` — so a verdict
resting on one observation is visible as one.

**Agreement with the recorded grade is a second, independent source, joined by
constraint id.** `trials_labelled` counts the trials on which *this constraint* was
decided both by the re-check and by the live run that wrote the bundle, and
`agreed_with_recorded_pass` how many of those the two agree on. One number is the
constraint verdict recomputed now, the other the verdict the live run froze into
`grade.yaml`'s `trace_check_results`.

A trial is not labelled where there is nothing to compare: the re-check was undecided,
the recorded verdict was undecided, the bundle was never graded, or the bundle recorded
no verdict for this id at all — which is the ordinary case under `--constraints`, where
the block names constraints the pack never had. So `trials_labelled` can be well short
of `trials_decided`, and the evidence block is where to look before reading anything
into that.

The trial-level `binary_pass` is reported per trial, in the report's `trials` rows,
never as a constraint's comparand: a trial fails for reasons beyond any one constraint,
so a constraint verdict compared against it would count a disagreement whenever some
*other* component failed the trial.

## Output

Artifacts land under `<source>/trace_replay/<replay_id>/`, a deliberate sibling of
judge replay's `replays/` — both names being reserved from discovery is what keeps
neither command reading the other's output:

- `trace_replay_report.yaml` — the run-level report: per-trial rows (each carrying the
  trial's `provenance`, `tool_log_present` and the `binary_pass` the live run
  recorded), the discrimination table, the evidence block, and the supplied block's
  gate notes.
- `<bundle-rel-path>/trace_checks_result.yaml` — the recomputed `TraceChecksResult`
  per re-checked bundle, under the bundle's discovered relative path.

**The read-only guarantee, stated precisely: no file that existed under `--source`
before the run is modified or removed.** Not "nothing under `--source` is written" —
that is false by construction when `--source` *is* a single bundle directory, because
the output subtree is then created inside it. `--dry-run` writes nothing at all.

`trace_checks_result.yaml` is deliberately a name no trial bundle already holds, so a
write that escaped the output subtree would create a file rather than clobber one. The
guarantee is scoped to that subtree, which is why `--replay-id` is held to a single
directory name: a separator or `..` in it would name a tree outside `--source`
altogether.

## From Python

The command is two calls — the batch, then the report over its outcomes:

```python
from pathlib import Path

from tolokaforge.core.grading.trace_replay import (
    declared_trace_checks,
    emit_trace_replay_report,
    run_trace_replay_batch,
)

source, replay_id = Path("output/<run-id>"), "candidate-3"
outcomes = run_trace_replay_batch(source, replay_id=replay_id)
report = emit_trace_replay_report(
    outcomes,
    declared=declared_trace_checks(outcomes),
    source=source,
    replay_id=replay_id,
)
```

**`declared_trace_checks(outcomes)` is the one supported way to build `declared`.**
The report needs the block each bundle was measured against, and that is not derivable
from the verdicts: a route that lost every trial has its constraints evaluated zero
times, so a report built from the results alone would drop exactly the rows an author
needs to see as `not_measured`. Reading `task.yaml` back for it would be a second
source of truth, and would answer the *pack's* block for a bundle re-checked against
`--constraints`. Each outcome carries the config it was replayed with; the projection
reads them off the batch that produced them.

Pass `dry_run=True` to the batch for the `--dry-run` shape, and use
`build_trace_replay_report` in place of `emit_…` to get the report without writing
it. Both return `None` where there was nothing to report.

Both raise `TraceReplayReportError` rather than reporting something they cannot stand
behind: a `declared` mapping missing a bundle the batch re-checked or naming
constraints the batch never evaluated did not come from this batch, and a corpus in
which two bundles claim one `task_id` while declaring *different* blocks has no single
row to be reported in — point the source at one revision of the pack, or supply
`--constraints` to measure one block over all of it.

## The authoring loop

```bash
# 1. Write the candidate constraint into a bare block. Tool names and argument
#    addresses are checked against what the bundles recorded, so they are the ones
#    the trials actually called.
cat > candidate.yaml <<'YAML'
constraints:
  - id: the_catalog_was_read_before_the_code_was_posted
    description: "the reason code was read from the catalog before it was posted"
    require:
      before:
        left:
          quantifier: any
          match:
            kind: tool_call
            tool: { equals: http_request }
            args: { method: { equals: GET }, url: { contains: /reason-codes } }
        right:
          quantifier: first
          match:
            kind: tool_call
            tool: { equals: http_request }
            args: { method: { equals: POST } }
YAML

# 2. Re-check it over a recorded corpus that covers both the correct process and the
#    wrong ones. A typo in the block is refused here, before any trial is re-checked.
uv run tolokaforge retrace --source output/<run-id> --constraints candidate.yaml

# 3. Read the row. `discriminating` means the corpus proved the constraint separates
#    processes. `always_true` / `always_false` means it did not — the constraint, the
#    corpus, or both need work. `never_decided` means the corpus cannot answer:
#    check the evidence line before blaming the constraint.

# 4. Commit only what discriminated, into the pack's `grading.yaml`.
```

A corpus that covers only the correct process cannot show a constraint discriminating
— everything passes. The recorded runs worth pointing `retrace` at are the ones that
reach the same end state by different processes, which is what the constraint exists to
tell apart.
