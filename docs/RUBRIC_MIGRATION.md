# Rubric Migration — retiring a judge criterion against recorded evidence

A rubric criterion that is really a trajectory predicate should be a
[`trace_checks`](GRADING.md#trace-checks) constraint: deterministic, free, and re-checkable
forever. `tolokaforge reconcile` is what makes that conversion *evidenced* rather than
asserted. It reads the migration a pack [declares](#the-declaration) and checks it against
trials whose judge already graded the criterion, recomputing the named constraints over each
recorded timeline and joining the two verdicts per trial.

It spends nothing — no agent, no environment service, no judge — and it never edits a pack.
The guarantee is structural: `tolokaforge/core/grading/rubric_migration.py` reaches the
trace-replay reader, the one production trace evaluator, the pure agreement maths and the task
loader `tolokaforge validate` already uses, and stops there, which a clean-subprocess import
probe holds (`tests/canonical/test_rubric_migration.py::test_the_differential_reaches_neither_an_llm_client_nor_the_judge`).

`reconcile` is a separate command from [`retrace`](TRACE_REPLAY.md) — which also spends
nothing — because their exit codes answer different questions. `retrace`'s exit code is about
the *readability of the corpus*: a constraint that discriminated nothing and a replay
disagreeing with the recorded grade both exit `0` there, deliberately. `reconcile`'s exit code
is the verdict of a *decision*, so it cannot live in a command whose zero means "every bundle
was readable". [Judge replay](JUDGE_REPLAY.md) re-runs the judge and spends judge tokens per
trial; neither of the free commands does.

## When to use it

- **Before taking a migration.** Declare the criterion as a `candidate`, point `reconcile` at
  a corpus of recorded trials, and read whether the constraint and the judge ever disagreed.
- **In CI, after taking one.** The constraint block is resolved from the *pack*, so editing a
  shipped constraint changes what is recomputed over the frozen corpus. A migration's evidence
  is therefore re-verified for free on every run and cannot rot silently.
- **When a criterion's text is half a code check.** The residue — the part no tool record can
  answer — is what a `narrowed` entry declares and the judge keeps reading.

## Usage

```bash
# Check every migration the packs behind a corpus declare.
tolokaforge reconcile --source tests/data/migration_corpora/notes_duplicate_check

# Resolve packs somewhere other than examples/ (repeatable).
tolokaforge reconcile --source <corpus> --packs tests/data/migration_packs

# Reconcile and report, writing no artifact.
tolokaforge reconcile --source <corpus> --dry-run
```

| Flag | Meaning |
|---|---|
| `--source` | The corpus: a run dir, a flat collection of bundle dirs, or a single bundle dir. A directory is a bundle iff it holds `task.yaml` + `trajectory.yaml`. |
| `--packs` | Directory searched recursively for the pack each bundle's `task_id` names; repeatable, default `examples/`. |
| `--replay-id` | Names the artifact subdirectory (letters, digits, `.`, `_`, `-`). Default: timestamped. |
| `--dry-run` | Reach the verdict and report it, write nothing. |

There is deliberately **no** `--constraints`-style flag. The block comes from the pack the
bundle's `task_id` resolves to, which is what makes the CI re-verification bite: a flag would
pin a fixture and the guard would be decorative.

A `task_id` that resolves in none of the searched roots, or in more than one, is an error
naming the id and the roots. So is a corpus holding no bundle, a pack whose declaration does
not load, and a pack whose constraints cannot be graded against the tool set a bundle
recorded — the last because a constraint naming a tool the corpus never had would fail on
every trial and read as a disagreement with the judge.

## The declaration

A task directory carries a `migration.yaml` beside its `grading.yaml`; the file's rules and
its load-time refusals are in [GRADING.md § Trace Checks](GRADING.md#trace-checks). What
`reconcile` reads from each entry:

| Field | Its role in the bar |
|---|---|
| `criterion` | Which recorded judge verdict is the reference label. |
| `by` | The constraints recomputed as the candidate label — a **conjunction**: all of them must pass. |
| `was` | The pre-migration criterion shape, verified against the rubric each bundle recorded. |
| `mode` | `candidate` / `narrowed` / `retired`. Decides which disagreement directions are tolerated. |
| `residual` | The author's claim about what remains. Rendered, never graded. |
| `acknowledged` | Waivers, each naming its trial and why the judge's verdict is the one to discount. |

## The bar

Three conditions, no magic number.

**Evidence — κ must be defined.** Cohen's κ over the joined labels is the evidence condition.
A corpus with no label variation has total chance agreement, so κ is undefined while accuracy
reads `1.0`; that is `insufficient_evidence` and never a pass. There is no threshold on κ's
*value* — κ is reported.

**Direction — because κ cannot see it.** κ is identical for a permissive and a strict
disagreement of the same count, so direction is a condition of its own:

| Direction | What it means | `candidate` | `narrowed` | `retired` |
|---|---|---|---|---|
| **strict** | judge met, constraint failed — the constraint is not even a necessary condition of the criterion | refused | refused | refused |
| **permissive** | judge not met, constraint passed — something the criterion asked for is no longer checked | tolerated | tolerated | refused |

A permissive disagreement is the *expected* shape of a narrow: the constraint checks one
conjunct where the criterion asked for two. Each one is reported per trial with the judge's own
justification, never as an aggregate.

**Acknowledgement.** A disagreement is waived only by naming its trial and a reason. An
acknowledgement naming a trial that no longer disagrees — because it agrees now, or because it
is not under `--source` — is **refused**, not silently ignored: a standing waiver would waive
the next disagreement on that trial unread.

### Verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `no_counter_evidence` | No trial in this corpus contradicted the claim, over N observations. | the only one that is `0` |
| `insufficient_evidence` | κ is undefined, so the corpus carries no evidence either way. | non-zero |
| `refused` | Something about the declaration is contradicted. | non-zero |

Exit `0` means every `narrowed` / `retired` entry reached `no_counter_evidence` and every
bundle was readable. A `candidate` entry's verdict is reported and gates nothing: it converts
nothing, retires nothing and changes no grade.

### What an entry is refused for

1. **An unacknowledged disagreement in a direction the mode does not tolerate.**
2. **A stale acknowledgement** — a waiver whose disagreement is gone.
3. **A `was` block the recorded rubric contradicts**, naming the bundle and the field.
4. **A corpus straddling a pack revision** — two bundles recording different shapes for one
   criterion, naming both.

### Why `was` is checked against the bundle and not the pack

`was` is a claim about the criterion's **pre-migration** shape, and every other rule keys on
it — an unverified `was.required: false` escapes the veto-preservation rule outright. The pack
holds only the *post*-migration state, and for a `retired` criterion nothing at all, so the
only surviving record of the pre-migration shape is the rubric each contributing bundle
recorded. Checking there closes the bypass an author opens by changing the declaration and the
pack in one commit: no load-time source holds the old `required`, and the bundle does not move
with them.

It also catches a rot mode nothing else would. A corpus **regenerated after the migration**
records the *post*-migration rubric, so the evidence silently re-bases onto the state the
migration itself produced. Because the fix is counter-intuitive — *keep the old bundles* — the
refusal says so in words: **"the corpus records the post-migration rubric; evidence for a
migration must come from bundles recorded before it."**

## Which trials contribute an observation

A trial contributes a pair of labels, or it contributes nothing and the report says why. Each
exclusion is reported with its reason, so a shrinking denominator is visible rather than a
corpus that quietly got smaller.

| Exclusion | Cause |
|---|---|
| `criterion_absent_from_recorded_rubric` | The rubric that bundle recorded held no such criterion — asked first, because such a bundle also records no verdict and the provenance answer is the useful one. |
| `judge_did_not_complete` | `judge_status` is anything but `completed`. An errored judge reached no verdict; folding it in as not-met would manufacture agreement with a failing constraint. |
| `no_criterion_results` | The recorded grade carries no per-criterion breakdown. |
| `no_verdict_for_criterion` | The grade holds no verdict for the named criterion. |
| `constraint_verdict_unavailable` | A named constraint was undecided, or was route-scoped and its route did not win. |

A bundle that cannot be **read** is different again: it is reported apart from the exclusions
and it blocks, because a verdict over a corpus that partly failed to load is a verdict over an
unknown denominator.

## Pooling across tasks

Two tasks may quote one measurement only when they claim the same criterion *and* recompute it
the same way — the same `was.description` (compared as words, so YAML wrapping does not
matter) and byte-identical resolved `by` constraints. Otherwise the run is refused naming both
tasks: a shared criterion id over two different predicates is two measurements folded into one
row, which is the fold `retrace`'s `(task_id, constraint_id)` keying exists to prevent.

## Output

```
<source>/reconcile/<replay_id>/reconcile_report.yaml
```

One report per reconciliation. Nothing else under `--source` is opened for write, and no pack
is touched whatever the verdict. Per entry the report carries `observations`, the **2×2
contingency table**, `accuracy`, `kappa`, both disagreement lists with the judge's own
`justification` per trial, every exclusion with its reason, the **counterfactual** below, the
verdict, and every refusal. The report names which side of the pair is the reference — the
maths is symmetric and will not say, so the artifact does.

## The counterfactual: what the migration does to each trial's verdict

Beside the evidence, `counterfactual` reports what the migration *would have done* to every
trial that contributed an observation. Per trial:

| field | what it is |
|---|---|
| `judge_component_before` / `_after` | the judge component the trial was graded on, and the one the reduced rubric produces |
| `score_before` / `_after`, `binary_pass_before` / `_after` | the trial verdict, before and after |
| `weights_before` / `_after` | the `combine.weights` map the trial was graded under, and the one the migration folds under |
| `vetoes_before` / `_after` | what could veto the trial: required criterion ids, plus the entry's `severity: gate` constraints after |

The *before* columns come from the bundle's own `grade.yaml`. The *after* columns are recomposed
by the same function the runner folds a live trial through
([`docs/GRADING.md`](GRADING.md#score-combination) § Score Combination) over the reduced rubric,
the recomputed trace component and — **the map the entry declares, never the map the pack holds
today**. That is the point: the pack's current map *is* the post-migration state a reviewer is
being asked to judge, so folding under it would answer the wrong question. Where the entry
declares no map, which the [freed-share rule](GRADING.md#trace-checks) permits only for a
criterion carrying no score share, the *after* map is the map that trial was graded under, since
nothing was freed to move.

**The recomposition is checked against the recorded verdict before any *after* column is
believed.** A trial whose recorded verdict the composition cannot reproduce is listed under
`unrecomputed_trials` with the divergence spelled out, and carries no before/after row: a
composition that cannot reproduce what the runner already decided says nothing trustworthy about
what the migration would decide. The same list names a trial whose recorded grade carries a
`state_checks` component — the runner folds that one from several sources and no single field
holds it, so it cannot be routed back through the fold that produced it.

**Nothing reads the counterfactual.** No verdict, no exit code and no refusal. It is evidence a
reviewer reads, and that is deliberate: gating on it would infer an unbounded safety property
from a finite corpus, which is the inference [the bar](#the-bar) refuses. The freed-share rule is
therefore satisfied by *declaring* a map, and this is where the declared map is measured — an
entry declaring the identity map on a criterion the judge scored below `1.0` reports the judge
component **rising**, which is the accepted residual made visible rather than trusted.

## Reading the evidence

The 2×2 table is required rather than optional, and it is the part to read first. A corpus
whose mass sits in one or two cells is *visibly* a designed experiment; an accuracy of `1.0`
read on its own says the opposite. On the committed corpus under
`tests/data/migration_corpora/notes_duplicate_check/not_met/`, all five observations sit in
`judge_not_met_constraint_failed`: accuracy `1.0`, κ `null`, verdict `insufficient_evidence`.

**The bar can only ever report absence of counter-evidence.** `no_counter_evidence` means "no
trial in this corpus contradicted the claim, over N observations" — never that the constraint
is equivalent to the criterion. The verdict's name, the required table and the author's
`residual` claim exist to keep that visible.

**Nothing here ranks one mode above another, and nothing can.** Zero disagreements in either
direction satisfies `retired`'s condition *and* `narrowed`'s, so the evidence cannot choose
between them: the mode is the author's recorded judgment and the `residual` claim is its
justification. The report model carries no field that ranks or compares modes, which is locked
structurally by set-equality over its field names rather than by prose — an assertion that no
*sentence* recommends a mode passes on every implementation and can never fire.

**And the experimental-design limitation, stated rather than left to be noticed:** a corpus
whose met half was produced by a prompt instructing exactly what the constraint measures
cannot distinguish "the constraint matches the criterion" from "the agents did what they were
told." A corpus of organically-varying trials would be stronger evidence and does not exist
for any shipped pack yet (#793).

## From Python

```python
from pathlib import Path

from tolokaforge.core.grading.rubric_migration import reconcile_corpus

report = reconcile_corpus(
    Path("tests/data/migration_corpora/notes_duplicate_check"),
    replay_id="ci",
    packs=[Path("examples")],
    dry_run=True,
)
for reason in report.blocking:
    print(reason)
```

`reconcile_corpus` raises `ReconcileError` for every defect that is a property of the
invocation; a defect in one declaration is a refusal on that entry instead, so the run still
reports every other entry. `ReconcileReport.blocking` is the exit contract: empty is what exit
`0` means.

The bar itself is pure — `reconcile_entry(entry, task_ids=…, trials=…)` decides every rule
from a sequence of `TrialEvidence`, which is what makes the degenerate corpora (one trial, no
contributing trial, every label identical) testable without recording any.
