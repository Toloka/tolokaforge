# Rubric Migration — retiring a judge criterion against recorded evidence

A rubric criterion that is really a trajectory predicate should be a
[`trace_checks`](GRADING.md#trace-checks) constraint: deterministic, free, and re-checkable
forever. `tolokaforge reconcile` is what makes that conversion *evidenced* rather than
asserted. It reads the migration a pack [declares](#the-declaration) and checks it against
trials whose judge already graded the criterion, recomputing the named constraints over each
recorded timeline and joining the two verdicts per trial.

It spends nothing — no agent, no environment service, no judge — and it never edits a pack.
The guarantee is structural: `tolokaforge/core/grading/rubric_migration.py` reaches the
trace-replay reader and its bundle discovery, the one production trace evaluator, the outcome
classifier a run's own attribution uses, the pure agreement maths and the task loader
`tolokaforge validate` already uses, and stops there, which a clean-subprocess import
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
- **In CI, after taking one.** `tolokaforge reconcile --dry-run` with no `--source` re-verifies
  every declaration in the tree over the corpus each one names. The constraint block is
  resolved from the *pack*, so editing a shipped constraint changes what is recomputed over the
  frozen corpus: a migration's evidence is re-verified for free on every run and cannot rot
  silently.
- **When a criterion's text is half a code check.** The residue — the part no tool record can
  answer — is what a `narrowed` entry declares and the judge keeps reading.

## Usage

```bash
# Every declared migration, each over the corpus its own declaration names. The CI
# invocation: it writes nothing, so it needs --dry-run and takes no --replay-id.
tolokaforge reconcile --dry-run

# Check every migration the packs behind one corpus declare. --dry-run because this corpus is
# committed: the report lands under --source, so without it the run dirties the tree.
tolokaforge reconcile --source tests/data/migration_corpora/notes_duplicate_check --dry-run

# Over a corpus of your own, keeping the report artifact.
tolokaforge reconcile --source results/<run-id>

# Resolve packs somewhere other than examples/ (repeatable).
tolokaforge reconcile --source <corpus> --packs tests/data/migration_packs
```

**With no `--source` the command sweeps the declarations instead of a corpus.** It resolves
every `migration.yaml` under `--packs` and reconciles each entry over the corpus that entry
itself names, one report per corpus, ordered by path. Two entries of one pack naming two
corpora are two rows over two bodies of evidence; two packs claiming one criterion the same
way are still [pooled](#pooling-across-tasks) into one. The sweep writes nothing at all, so
`--replay-id` and an invocation without `--dry-run` are **refused** rather than ignored: both
ask for a report that will not exist. The exit code is the same gate as below, over every row.

**Reconciling a committed corpus wants `--dry-run`.** The report is written *under* `--source`
(see [Output](#output)), so a run over anything tracked by git leaves a `reconcile/` directory
behind in it. `.gitignore` covers the replay commands' output directories under any root, so
such a run is not silently committed — but a dry run is what a verdict-only invocation wants,
and it reaches exactly the same verdict.

| Flag | Meaning |
|---|---|
| `--source` | The corpus: a run dir, a flat collection of bundle dirs, or a single bundle dir. A directory is a bundle iff it directly holds `trajectory.yaml` ([JUDGE_REPLAY.md](JUDGE_REPLAY.md#what-gets-re-judged)). A discovered bundle recording a trial that never ran carries no `task.yaml`, names no pack, and is **excluded from the corpus by name** — the report's `excluded_bundles` — rather than blocking it. Omitted: every declared migration, each over the corpus it names. |
| `--packs` | Directory searched recursively for the pack each bundle's `task_id` names, and for the declarations the sweep reconciles; repeatable, default `examples/`. |
| `--replay-id` | Names the artifact subdirectory (letters, digits, `.`, `_`, `-`). Default: timestamped. Refused without `--source`. |
| `--dry-run` | Reach the verdict and report it, write nothing. Required without `--source`. |

There is deliberately **no** `--constraints`-style flag. The block comes from the pack the
bundle's `task_id` resolves to, which is what makes the CI re-verification bite: a flag would
pin a fixture and the guard would be decorative.

A `task_id` that resolves in none of the searched roots, or in more than one, is an error
naming the id and the roots. So is a corpus holding no bundle, a pack whose declaration does
not load, and a pack whose constraints cannot be graded against the tool set a bundle
recorded — the last because a constraint naming a tool the corpus never had would fail on
every trial and read as a disagreement with the judge.

A pack with **no grading block on disk** — naming no source at all (no `grading:` field and
no sibling `grading.yaml`), or naming a path with no file at it — is an error too, naming the
pack. It is refused under every `adapter_type` a pack may declare, which is stricter than
[`tolokaforge validate`](CLI.md#task-validation), where the same absence passes with a `?`
line for any adapter that resolves its own grading config: the migration is declared beside
that file and the `trace_checks` block it names is what each recorded verdict is recomputed
from, so a config resolved without a file leaves a corpus with nothing to be reconciled
against.

## The declaration

A task directory carries a `migration.yaml` beside its `grading.yaml`; the file's rules and
its load-time refusals are in [GRADING.md § Trace Checks](GRADING.md#trace-checks). What
`reconcile` reads from each entry:

| Field | Its role in the bar |
|---|---|
| `criterion` | Which recorded judge verdict is the reference label. |
| `by` | The constraints recomputed as the candidate label — a **conjunction**: all of them must pass. Its route-scoped ids all sit in **one** route, since a trial is scored on the route it took; shared ids accompany any route's. |
| `corpus` | The committed corpus the claim is measured over. **Required in every mode**, and it must resolve — see below. |
| `was` | The pre-migration criterion shape, verified against the rubric each bundle recorded. |
| `mode` | `candidate` / `narrowed` / `retired`. Decides which disagreement directions are tolerated. |
| `residual` | The author's claim about what remains. Rendered, never graded. |
| `evidence` | What the recorded verdicts measured. `observations` and `kappa` are checked against what this run measures — [refusal 5](#what-an-entry-is-refused-for). Required for `narrowed` / `retired`, forbidden on a `candidate`: it is the measurement a *decision* rests on, and a candidate has decided nothing. |
| `acknowledged` | Waivers, each naming its trial and why the judge's verdict is the one to discount. The trial is a bundle under the entry's `corpus`, in every mode. |

**The corpus resolves, or the declaration is refused at load.** A value names a directory
carrying the `corpus.yaml` [`tolokaforge curate`](#building-a-corpus) writes, or one whose
immediate subdirectories all do — exactly one level, which is the multi-part shape that
command can write. The value is **relative**, and an absolute one is refused: it would win the
join outright and resolve to itself whatever base was supplied, which would stop the corpus a
run reads from being a property of the declaration.

It is read against a **base the caller supplies**: unset, the declaration's own directory, so
a corpus travels with an external pack; `tolokaforge validate` and `tolokaforge reconcile`
pass the working directory, which is what the shipped repository-root-relative values are
written from. The refusal names the resolved path and the base separately, so a run from the
wrong directory says so rather than reporting a corpus nobody wrote. **The residue:** both
shipped commands override the default, so a pack whose `corpus` is written relative to its own
directory has no shipped invocation that resolves it today — the default is what a Python
caller gets, and what a command giving an external pack its own base would need.

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

Before any of these, at load: **a `by` whose route-scoped ids sit in different `alternatives`
routes.** A trial is scored on the route it took, so the verdicts a reconciliation recomputes
carry the shared constraints and the winning route's alone; a conjunction over two routes has
no verdict for one of its ids on every trial and reaches no observation on any corpus. The
refusal names both ids with the route each sits in. What a claim about two routes would need
instead is **#1057**.

Then, against the corpus:

1. **An unacknowledged disagreement in a direction the mode does not tolerate.**
2. **A stale acknowledgement** — a waiver whose disagreement is gone.
3. **A `was` block the recorded rubric contradicts**, naming the bundle and the field.
4. **A corpus straddling a pack revision** — two bundles recording different shapes for one
   criterion, naming both.
5. **A declared `evidence` block the measurement contradicts.** `evidence.observations` and
   `evidence.kappa` are what a reviewer reads *instead of* re-running the command, so they are
   the measurement or they are nothing. A run measuring **more** observations than the
   declaration counted has read a corpus the declaration under-counts, and one reaching the
   declared count must reproduce the declared κ, compared at three decimals — the precision
   the report prints, and therefore the number an author copies.

   **How far short of the declared count is charged depends on which invocation read it.**
   Given a `--source`, the rule is a **bound**: the source may deliberately be part of the
   corpus the declaration names — pointing at one arm of a two-arm corpus is how each half is
   shown to be the other's falsifier — so a run measuring fewer observations says nothing.
   Sweeping the declarations (no `--source`), it is an **equality**: the source *is* the
   corpus the entry names, there is no part of it left out, and a count below the declared one
   means bundles went missing. The residue the bound leaves — a declaration over-*counting* its
   own corpus, indistinguishable from a reconciliation over a subset — is therefore closed for
   the one invocation that can close it, which is the one CI runs.

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

A bundle that cannot be **read** is different again: it is reported apart from the
per-criterion exclusions above and from the corpus-level `excluded_bundles` (the task-less
bundle a trial that never ran leaves, excluded by name), and it blocks, because a verdict over
a corpus that partly failed to load is a verdict over an unknown denominator. It never aborts
the run, whatever is wrong with it — bytes no decoder accepts in any of its artifacts, a
`task.yaml` absent on a bundle whose trajectory records a real episode, a `task.yaml` whose
recorded rubric does not read as one, a `grade.yaml` whose `criterion_results` hold a row that
is not a judge verdict or whose `score` or component values are not numbers. Each is one named
entry under `unreadable_trials`, and every other trial in the corpus is still measured.

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
| `judge_component_before` / `_after` | the judge component the trial was graded on, and the one the reduced rubric produces. `_after` is `null` where the reduced rubric holds no criterion at all — a judge scoring nothing has no component |
| `score_before` / `_after`, `binary_pass_before` / `_after` | the trial verdict, before and after |
| `weights_before` / `_after` | the `combine.weights` map the trial was graded under, and the one the migration folds under |
| `vetoes_before` / `_after` | what could veto the trial: required criterion ids, plus the entry's `severity: gate` constraints after. Mode-aware — see below |

**The component and score columns are mode-blind; the veto set is not.** The *after* judge
component drops the criterion from the judge's side whatever `mode` says, so for a `narrowed`
entry it is **the bound of a full retirement** rather than the narrow's own effect. That is the
only projection a recorded corpus can support: the narrowed text has no recorded label anywhere,
because every trial was graded against the text it replaced, so what the judge *would* award for
the residue is not computable from the evidence — only what it awards for nothing at all is. Read
the after columns as "at most this much moves", and the shipped narrow's rows accordingly.

The veto set is mode-aware because requiredness is **declared** rather than judged, so the
report can state it exactly. A `narrowed` criterion stays in the rubric and stays
`required: true`, so `vetoes_after` carries **both** it and the trace gate — two vetoes over one
policy, which is the shipped narrow's whole shape and what a set omitting the kept one would
misreport as a veto lost. A `retired` criterion is gone, so its veto goes and the gate is what
holds one; a `candidate`'s counterfactual projects the retirement it is a candidate *for*, so it
reads the same way.

The *before* columns come from the bundle's own `grade.yaml`. The *after* columns are recomposed
by the same function the runner folds a live trial through
([`docs/GRADING.md`](GRADING.md#score-combination) § Score Combination) over the reduced rubric,
the recomputed trace component and — **the map the entry declares, never the map the pack holds
today**. That is the point: the pack's current map *is* the post-migration state a reviewer is
being asked to judge, so folding under it would answer the wrong question. Where the entry
declares no map, which the [freed-share rule](GRADING.md#trace-checks) permits only for a
criterion carrying no score share or a `mode: candidate`, the *after* map is the map that trial
was graded under, since nothing was freed to move.

**The recomposition is checked against the recorded verdict before any *after* column is
believed.** A trial whose recorded verdict the composition cannot reproduce is listed under
`unrecomputed_trials` with the divergence spelled out, and carries no before/after row: a
composition that cannot reproduce what the runner already decided says nothing trustworthy about
what the migration would decide. The same list names a trial whose recorded grade carries a
`state_checks` component — the runner folds that one from several sources and no single field
holds it, so it cannot be routed back through the fold that produced it.

**A column folding a component its map does not weight is the third such gap**, and it is the
one an entry declaring no map runs into: the *after* column installs `trace_checks` as a scored
component, and the map the trial was graded under cannot weight a check the migration has not
made yet. The fold refuses to invent a share, so the row names the missing weight key instead of
reporting a number that would carry exactly the defect this report exists to measure. Declaring
`combine_weights` — with a share for the check the conversion installs — is what makes the
counterfactual computable for such an entry.

**Nothing reads the counterfactual.** No verdict, no exit code and no refusal. It is evidence a
reviewer reads, and that is deliberate: gating on it would infer an unbounded safety property
from a finite corpus, which is the inference [the bar](#the-bar) refuses. The freed-share rule is
therefore satisfied by *declaring* a map, and this is where the declared map is measured — an
entry declaring the identity map on a criterion the judge scored below `1.0` reports the judge
component **rising**, which is the accepted residual made visible rather than trusted.

## Which packs get a corpus

**A pack gets a committed corpus exactly when it declares a `migration.yaml` entry.** The
entry names the corpus, the corpus's composition is the manifest `tolokaforge curate` wrote
into it, and `tolokaforge reconcile` with no `--source` re-verifies every one of them over the
packs a reviewer reads.

Most shipped packs carrying a rubric declare no migration, and they need no corpus. Such a
pack asserts that none of its trace constraints claims any of its criteria — a claim a
reviewer reads off the absence of the file, and one with nothing to triage against recorded
verdicts. Declaring an entry is what turns that into a claim about evidence.

**The signal to re-curate is a refusal, not a diary note.** Editing the rubric a `candidate`
declares `was` against refuses the declaration at load, so a criterion whose text moved cannot
keep a stale claim. Regenerating a corpus *after* taking the migration refuses it at reconcile
with `recorded_rubric_contradicts_was`, because the fresh bundles record the post-migration
rubric — see [Why `was` is checked against the bundle](#why-was-is-checked-against-the-bundle-and-not-the-pack).

## Building a corpus

`tolokaforge curate` turns recorded runs into a corpus. It spends nothing, writes only under
`--into`, and states the composition it chose in the corpus's own `corpus.yaml`, so a reader
can check the choice after the run directories behind it are gone — `results/` is gitignored,
and the runs behind the corpus committed here are on nobody's disk.

```bash
# One half of a corpus, from three runs of the same arm.
tolokaforge curate --criterion checked_duplicates_first \
  --source results/<run-a> --source results/<run-b> --source results/<run-c> \
  --into tests/data/migration_corpora/<criterion>/met --dry-run
```

| Flag | Meaning |
|---|---|
| `--source` | A recorded run dir (`trials/<task>/<idx>` subtree) or a single bundle dir; repeatable, because a corpus is usually assembled from several runs. Discovery is the one bundle identity every offline command uses: a directory is a bundle iff it directly holds `trajectory.yaml`. |
| `--into` | The corpus directory to write. |
| `--criterion` | The rubric criterion id whose recorded verdicts the corpus carries. |
| `--exclude` | `<bundle-dir>=<reason>`; repeatable. The author's own judgment about one bundle, recorded in the manifest as `by: author` with the reason. |
| `--replace` | Rewrite the whole `--into` directory. Without it, a destination already holding a `corpus.yaml` is an error — a corpus is never a half-refresh. |
| `--dry-run` | Classify every discovered bundle and report, writing nothing. |

**A bundle is admitted iff** it carries `task.yaml`, `trajectory.yaml` and `grade.yaml`; its
`grade.yaml` records `judge_status: completed`; its `criterion_results` holds a verdict for
`--criterion`; and it is not environment-dead. Every bundle that is not admitted is named on
the console and in the manifest with its reason and the observation behind it. A run that
admits nothing writes nothing and exits non-zero, naming the sources it searched.

**Environment-dead is defined on the record, not on rendered text.** A bundle is
environment-dead iff it carries a `tool_log.yaml`, that record holds at least one call, and
**no** call has `status: success`: the trial's tools never worked and the judge scored a
transcript of failures. `TraceEvent.status` comes from that record alone, and the `role: tool`
message's `Error: ` prefix is harness-rendered rather than a field — so a **record-less**
bundle is *not* rejected by this rule, because the claim would need evidence the bundle does
not carry. Each admitted bundle's manifest entry carries `record_carried`, which says where
the rule could reach.

**The write set is the five files a differential reads** — `task.yaml`, `trajectory.yaml`,
`grade.yaml`, `tools_schemas.yaml`, `metrics.yaml` — **plus `tool_log.yaml` wherever the
source bundle had one**. A corpus is record-carrying exactly when its sources were, which is
what a constraint reading `status`, `executor` or `latency_seconds` needs (see
[The committed corpora](#the-committed-corpora)). Bundle directories are named
`<run-id>_trial<index>`, uniformly; one run's two tasks share a trial index, so curating both
into one corpus is refused rather than silently collapsed.

`corpus.yaml` names the criterion, the task ids, the source runs, the curation time, every
admitted bundle (its directory, source run, task id, agent and judge models, the judge's
recorded `met`, and whether a record travelled with it) and every rejection (its source path,
reason, `by: rule | author` and the observation behind it — `tool_calls: 6, succeeded: 0`).

**A multi-part corpus is a directory whose subdirectories are corpora**, each written by its
own invocation with its own `--into`. Nothing in the command knows about halves: `reconcile`
discovers bundles recursively, so pointing it at the parent or at one part both work, and a
one-sided part stays a corpus a reader can point a command at on its own.

## The committed corpora

Two corpora ship, one per declared migration, and they reach opposite verdicts. The notes
corpus is evidence a narrow rests on; the `lot_ops` corpus refuses the candidacy it was
generated for. Both are written by `tolokaforge curate` and re-verified in CI over the packs
a reviewer reads.

### `notes_duplicate_check` — the narrow's evidence

`tests/data/migration_corpora/notes_duplicate_check/` is the repo's judge-labelled corpus:
seventeen recorded trials of the notes duplicate-check criterion, in two halves, committed with
plain git at ~13 KB per bundle. It is a multi-part corpus in the sense above — each half is a
corpus written by its own `tolokaforge curate` invocation, carrying its own `corpus.yaml`.

| half | trials | task | the judge's `checked_duplicates_first` | tool-call record |
|---|---|---|---|---|
| `not_met/` | 5 | `notes_add_note_duplicate_check_gated` | not met on every one | none |
| `met/` | 12 | `notes_add_note_duplicate_check_policy` | met on every one | every bundle |

**Its composition is a file a machine wrote and a machine checks.** Each half's `corpus.yaml`
names every bundle under it with the label its own `grade.yaml` recorded and the models that
produced it, and every discovered trial that did *not* enter, with the observation behind the
refusal. Canonical tests read the manifest against the tree in both directions and re-read each
label off the bundle it describes, so a corpus that has drifted from its own account of itself
reds rather than reconciling quietly.

Both halves are reproducible by command, over runs under the gitignored `results/`:

```bash
tolokaforge curate --criterion checked_duplicates_first \
  --source results/native_shared_domain_policy_demo_20260803_062316 \
  --source results/native_shared_domain_policy_demo_20260803_063010 \
  --source results/native_shared_domain_policy_demo_20260803_063150 \
  --into tests/data/migration_corpora/notes_duplicate_check/met --replace

tolokaforge curate --criterion checked_duplicates_first \
  --source results/native_shared_domain_example_20260629_133126 \
  --source results/native_shared_domain_example_20260702_140836 \
  --source results/native_shared_domain_gate_demo_20260625_184817 \
  --source results/native_shared_domain_gate_demo_20260626_101928 \
  --source results/native_shared_domain_gate_demo_20260626_102829 \
  --source results/native_shared_domain_gate_demo_20260804_122027 \
  --into tests/data/migration_corpora/notes_duplicate_check/not_met --replace
```

**Twenty-one graded trials exist and seventeen are committed; the four that are not are
refused by rule, and the manifests say so.** The three `20260803_062316` trials (6, 6 and 5
recorded calls) and the `20260804_122027` gated trial (4) have every call at `status: error`:
the task's MCP server never started, so the judge scored a transcript of failures on a trial
whose environment was dead. The harness called those runs completed, `aggregate.json` reported
no harness errors, and nothing but the tool-call record separates them from an admissible
trial — which is why the sources they came from are named on the command line above rather than
left out of it. Leaving them out would leave the corpus's boundary unstated once `results/` is
gone.

**The `not_met/` half is deliberately heterogeneous in its agent, and that is the measured
finding rather than an untidiness.** Two of its five trials ran `anthropic/claude-sonnet-4-6` as
the agent and three ran `openai/gpt-4o-mini`; all five skipped `list_notes`. So the
stronger-model lever is **measured failed** rather than untried, which is why the second label
had to be bought with a prompt. The `met/` half is twelve `openai/gpt-4o-mini` trials. Each
bundle's `task.yaml` carries its own `model_config`, so the attribution travels with the corpus
rather than living only here.

**The two halves are the two arms of one experiment, and the independent variable is one
paragraph of system prompt.** The arms are two testcases of
`examples/native/native_shared_domain/`, byte-identical in rubric, trace constraint, weights,
initial state, user message and backstory. `add_note_duplicate_check_gated` inherits the shared system prompt, which
does not mention the check-first policy, so agents skip `list_notes` and the criterion is not
met. `add_note_duplicate_check_policy` ships **its own** `system_prompt.md`, carrying the shared
prompt verbatim with the policy paragraph appended, so a competent agent lists, warns, and the
criterion is met.

Two mechanics make that fork necessary rather than convenient, and both are measured. A
task-level `system_prompt` is a *path* resolved against the task directory, and `deep_merge`
lets a scalar from the delta **replace** the base — so a task-level value drops the shared
prompt outright instead of extending it. The fork is therefore guarded: a canonical assertion
requires the arm's prompt to *start with* the shared file's exact bytes, so a drift in
`_shared/system_prompt.md` reds rather than diverging silently. `startswith` is the assertion
because the paragraph is **appended**; interleaving it under one of the shared file's headings
would break containment on its first commit.

Over the union the entry reports **17 observations, accuracy `1.0`, κ `1.0`**, twelve
met/passed and five not-met/failed with nothing off the agreeing diagonal, and reaches
`no_counter_evidence` — exit `0`. Over `not_met/` alone the same declaration is
`insufficient_evidence`, because κ is undefined on one label. Each half is the other's
falsifier, and both verdicts are locked in
`tests/canonical/test_rubric_migration.py`.

Both arms carry the declaration of the narrow this corpus is the evidence for, so the default
`--packs examples/` resolves it: that is the CI re-verification, and it is over the pack a
reviewer reads rather than over a fixture. Their two `migration.yaml` files are byte-identical,
which is what keeps the pooled evidence one measurement quoted twice; a drift between them is a
pooling refusal.

Fixture packs under `tests/data/migration_packs/` declare the same narrow with **no**
`combine_weights`, reached with `--packs tests/data/migration_packs` — which is the only way the
counterfactual's *source* is measurable, since a report that folded under the pack's own map
would be indistinguishable there. They carry the shipped `task_id`s, so `tests/data` is
deliberately never a default root: a `task_id` resolving in two roots is an error.

**The halves differ in what a constraint can read over them, and that asymmetry is the
sources', not the curation's.** The five `not_met/` bundles are `schema_version: 1` and
record-less: their runs predate the persisted tool-call record, so `TraceEvent.status`,
`executor` and `latency_seconds` are `None` there and the environment-dead rule had nothing to
classify them by — their manifest entries say `record_carried: false`. All twelve `met/`
bundles carry `tool_log.yaml`, because all twelve source runs did. This corpus's constraint
reads neither `status` nor `result`, so the not-met half blocks nothing; a migration whose
constraint reads either needs a corpus that is record-carrying throughout, which is a property
of the runs it is curated from.

### `lot_ops_names_lot` — a candidacy its own corpus refuses

`tests/data/migration_corpora/lot_ops_names_lot/` holds ten recorded trials of `lot_ops_01`,
generated for the `names_lot` candidacy and **refusing it**. Both source runs are committed
run configs under `examples/native/multi_service_lot_ops/run_configs/`, one per arm:
`corpus_generation_haiku.yaml` (agent `anthropic/claude-haiku-4-5`) and
`corpus_generation_gpt_4o_mini.yaml` (agent `openai/gpt-4o-mini`), five repeats each, judge
`anthropic/claude-sonnet-4-6`, ten turns. An arm *is* a config file, because `RunConfig.models`
holds one model per role and `tolokaforge run` has no agent-model override.

**The independent variable is the agent model and nothing else, so the variation is organic.**
No arm's prompt instructs the behaviour the constraint measures — buying a label that way is
what the [design limitation](#reading-the-evidence) below describes, and it would make the
corpus evidence about the prompt rather than about the criterion.

| what | measured |
|---|---|
| observations | 10, five per arm, every bundle record-carrying |
| the judge's `names_lot` | met on all ten |
| `the_lot_was_read_before_the_action_was_opened` | failed on all ten |
| table | all ten in `judge_met_constraint_failed`, every other cell `0` |
| accuracy / κ | `0.000` / `0.000` |
| verdict | `refused` — and the command still exits `0` |

**Every observation is a strict disagreement, which no mode tolerates.** The judge found the
criterion met where the constraint failed, so the constraint is not even a necessary condition
of it. The reason is in the transcripts rather than in the corpus: each trial issued exactly
two HTTP calls — the reason-code catalog, then the POST — and never read the lot, because the
user's own message supplies `lot_id 7` and the task's guidance asks only for the catalog. The
constraint measures a step this task never asks for.

**A refused `candidate` is the bar working, not a build break.** A candidate converts nothing,
retires nothing and changes no grade, so its verdict is reported and gates nothing: the
reconciliation exits `0` while naming the refusal. Making a shipped candidacy fail the build
the moment its evidence arrived would be the opposite of what declaring one is for. What the
declaration should become — a different `by`, or none — is a decision for the pack's author,
and the corpus is what that decision now has to answer to.

**This pack's counterfactual carries no row at all.** Its grade includes a `state_checks`
component, which the runner folds from several sources and no single recorded field holds, so
the recomposition cannot reproduce the verdict the runner already reached. All ten trials are
listed under `unrecomputed_trials` with that reason, and no before/after projection exists —
the structural consequence, for a substrate-graded pack, of the rule that
[a projection is never believed](#the-counterfactual-what-the-migration-does-to-each-trials-verdict)
over a composition that cannot reproduce the recorded verdict.

Reproducible by command, over runs under the gitignored `results/`:

```bash
tolokaforge curate --criterion names_lot \
  --source results/lot_ops_corpus_haiku_20260812_132740 \
  --source results/lot_ops_corpus_gpt_4o_mini_20260812_133234 \
  --into tests/data/migration_corpora/lot_ops_names_lot --replace
```

## Reading the evidence

The 2×2 table is required rather than optional, and it is the part to read first. A corpus
whose mass sits in one or two cells is *visibly* a designed experiment; an accuracy of `1.0`
read on its own says the opposite. Point `reconcile` at
`tests/data/migration_corpora/notes_duplicate_check/not_met/` alone and all five observations
sit in `judge_not_met_constraint_failed`: accuracy `1.0`, κ `null`, verdict
`insufficient_evidence` — the same accuracy the union reports, and none of the evidence.

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
told." That is precisely how the [`met/` half](#notes_duplicate_check--the-narrows-evidence)
was produced, so the κ of `1.0` it yields is a property of the design as much as of the
criterion. The [`lot_ops` corpus](#lot_ops_names_lot--a-candidacy-its-own-corpus-refuses) buys
no label that way — its two arms differ in the agent model alone — and it is the corpus that
refuses its claim, which is the shape of evidence that can.

## From Python

```python
from pathlib import Path

from tolokaforge.core.grading.rubric_migration import (
    reconcile_corpus,
    reconcile_declared_corpora,
)

report = reconcile_corpus(
    Path("tests/data/migration_corpora/notes_duplicate_check"),
    replay_id="ci",
    packs=[Path("examples")],
    dry_run=True,
)
for reason in report.blocking:
    print(reason)

# Every declaration, each over the corpus it names — one report per corpus.
for swept in reconcile_declared_corpora(packs=[Path("examples")], corpus_base=Path.cwd()):
    print(swept.source, [entry.verdict.value for entry in swept.entries])
```

Both raise `ReconcileError` for every defect that is a property of the invocation; a defect in
one declaration is a refusal on that entry instead, so the run still reports every other entry.
`ReconcileReport.blocking` is the exit contract: empty is what exit `0` means. `corpus_base` is
the directory each declaration's `corpus` is read against — it defaults to the declaration's own
directory, and the CLI passes the working directory, so nothing under `tolokaforge/core/`
resolves a path off ambient state.

The bar itself is pure — `reconcile_entry(entry, task_ids=…, trials=…)` decides every rule
from a sequence of `TrialEvidence`, which is what makes the degenerate corpora (one trial, no
contributing trial, every label identical) testable without recording any.
