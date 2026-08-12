# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Changed

- **cli**: **a recorded trial a replay command cannot work with is a named skip, not a failure.** Both offline commands refused, at exit 1, bundles a run legitimately writes — `rejudge` one carrying no `grade.yaml`, `retrace` one carrying no `task.yaml` — reporting a trial that never produced the input as a defective one. Each now classifies what it is pointed at and says which shape it is, all three discover through one identity rule, and `rejudge`'s batch prints a census that accounts for every trial directory under `--source`.

  **`rejudge` and the grade-less bundle.** `tolokaforge rejudge --trial <bundle>` pointed at a directory holding no `grade.yaml` printed `failed … not a trial bundle: …/grade.yaml is missing or not a mapping` and **exited 1** — for both shapes a run legitimately writes without a verdict: a trial grading refused (`trajectory.yaml` carries a `grading_error`) and a trial the infrastructure aborted before it was measured. **This is a change to the CLI exit-code contract**: that invocation now prints `skip (no grade) <path> — <reason>` and exits **0**. The reason is read off the bundle's own `trajectory.yaml` through the run's own `classify_trial_outcome`, one sentence per outcome class, so the two grade-less shapes are told apart instead of both being labelled an abort: the refusal sentence carries the recorded `grading_error`, the abort sentence carries the `termination_reason`, and a grade-less bundle that is neither is reported as the anomaly it is rather than mislabelled. **It names the outcome class, not the operational cause** — why an environment did not come up is `error_reason` in the same bundle's `metrics.yaml`, and a decorative second read would have to either fail the skip or swallow its own errors. Classification runs **before** any input is reconstructed, so a grade-less bundle resolves no rubric and no judge model and cannot spend. Two neighbouring states deliberately stay `failed`: a `grade.yaml` that is *present* and unreadable (a bundle that cannot say what the run concluded is a different state from one that says nothing was graded), and a grade-less bundle whose `trajectory.yaml` is absent or unreadable, which fails naming that file. The summary's totals line gains its `skipped-no-grade` count, so it accounts for every per-trial line printed above it. Discovery is unchanged: a grade-less bundle is reached through `--trial`.

  **`retrace` and the task-less bundle.** A trial whose environment never came up is written by the executor alone — `trajectory.yaml` and `metrics.yaml`, no `task.yaml`, because the conductor never ran — and it is the most common infrastructure-abort shape on disk. `tolokaforge retrace --trial <bundle>` reported it as `failed: not a trial bundle: …/task.yaml is missing or not a mapping` and **exited 1**; it is now `skip (no task)` at exit **0** when the bundle's own trajectory classifies the trial an infrastructure abort, with the reason naming that class and its `termination_reason`. **The rule is narrow and the two neighbouring states stay `failed`**: a task-less bundle whose trajectory records a real episode has lost its task snapshot and fails naming `task.yaml`, and one whose `trajectory.yaml` is missing or unreadable fails naming that file. The new status is deliberately **not** folded into `skipped_not_applicable`, which asserts that the *pack* declares no `trace_checks` — a claim a bundle with no `task.yaml` cannot support — and for the same reason the run-level evidence gains its own `bundles_no_task` count instead of widening `bundles_skipped`: **`trace_replay_report.yaml` therefore carries one new field**. The disposition reaches all three places the console accounts for a batch — the per-bundle line, the totals, and the `Evidence:` block. Discovery is unchanged here too: a task-less bundle is reached through `--trial`.

  **One bundle identity, and the third consumer.** The two discovery functions that disagreed — `replay.discover_trial_bundles` keyed on `grade.yaml` + `task.yaml`, `trace_replay.discover_trace_bundles` on `task.yaml` + `trajectory.yaml` — are **deleted** and replaced by one rule in a new leaf module, `tolokaforge/core/grading/replay_layout.py`: **a directory records a trial iff it directly contains `trajectory.yaml`**, the only file every writer produces. `rejudge`, `retrace` and `reconcile` all discover through it, so a directory is a bundle for one exactly when it is a bundle for the others, and a bundle named with `--trial` gets the disposition discovery would have given it. The module reaches neither the judge nor an LLM client, which is what keeps the spend-nothing commands costing nothing on their import path. `rejudge` gains the `trace_replay/` exclusion it lacked, so neither command re-judges the other's output; `replays` and `trace_replay` stay reserved at any depth. **The measured effect**: over a run directory holding the five trial shapes the harness writes, `rejudge` reported 2 of 5 and `retrace` 4 of 5; both now report 5, and the two agree bundle for bundle. A batch's discovered total is therefore stable across two runs of one suite that differ only in how many trials aborted — the eligible count still moves, as it must, and the no-grade skips account for the difference by name. **`reconcile` adopts the rule in the same change**, because discovery is its only entry: a discovered bundle recording a trial that never ran carries no `task.yaml`, names no pack, and is excluded from the corpus by name through the new `ReconcileReport.excluded_bundles` (so `reconcile_report.yaml` gains a field, and the console names the count beside `trials_read`). It does **not** block — **`reconcile`'s exit code does not change** — while any *other* task-less bundle still lands in `unreadable_trials` and still blocks; a corpus of nothing but excluded bundles now raises naming that rather than reporting that no pack declares a migration.

  **The batch says what it discovered.** `rejudge`'s summary is a census — `<N> discovered: <e> eligible, <s> not-applicable, <g> no-grade, <f> failed` — over every bundle under `--source`, with bundle paths printed **relative to `--source`** as `retrace` already prints them. `replay_report.yaml` gains a `batch` block (`ReplayBatchCensus`) carrying the same five numbers, because `trials` covers only the *compared* subset and an operator diffing two runs of one suite needs the denominator: "fewer trials were judge-eligible" is a different fact from "the provider throttled us". The model refuses a census whose dispositions do not sum to `discovered`, so a report that cannot account for its own batch is never written. **The rule that no report is written when nothing replayed is unchanged** — such a batch has no comparison to make and its census is on the console. **Third exit-code change**: a `--source` that discovers **no bundle at all** now exits `1` naming the directory searched, matching `retrace`; it previously printed zeros and exited `0`, which is a reporter that claims nothing and returns success. A skip of either kind still never moves the exit code.

  **The whole user-facing change, in one list.** Exit codes: grade-less `rejudge --trial` `1` → `0`; task-less `retrace --trial` `1` → `0` for the abort shape; empty `rejudge --source` `0` → `1`. **`reconcile`'s exit code does not move** — what changes is that its report names trials it never previously saw. Artifacts: `replay_report.yaml` gains `batch`, `trace_replay_report.yaml` gains `bundles_no_task`, `reconcile_report.yaml` gains `excluded_bundles`. Consoles: a disposition line per bundle on all three commands, relative paths on `rejudge`, and the excluded count beside `trials_read` on `reconcile`.

- **grading**: **the `trace_checks` backstop reads both operands' JSON types, so a binding reference is diagnosed in whichever direction the mismatch runs — and the sentence it prints is rewritten.** The guard pre-gated on `isinstance(value, str)` and so answered in one direction only: a bound `int` against a text field was reported, while text bound against an `integer` argument was false on every trajectory *and silent*, and the constraint read `present is unmatched` — the message a genuine agent miss carries. It now asks `predicates.ever_satisfiable(operator, json_type_of(value), json_type_of(bound))`, the per-operator table `tolokaforge validate` has held since #781, and pre-gates on both types being nameable itself, because that function fails open on a type it cannot name. A pair the table admits is not a mismatch and is not reported: an `integer` field against a `number` binding compares as `1 == 1.0` and is scored against the agent like any other comparison. **The message crosses the wire**, as `TraceConstraintResult.message` is proto field 5 and lands in `grade.yaml`, so a consumer matching on its text reads new wording:

  ```text
  the args.code comparison was not made: binding 'delivery' holds 4021, a JSON integer, and no candidate carried a value at that field which equals_binding can ever satisfy against it — two JSON types the operator cannot pair are false on every trajectory, whichever of the two holds the text. Reference the binding from an args predicate whose arguments the tools type the same way, or extract a regex capture off a field that holds text
  ```

  It names the binding's JSON type where it named the Python type name of the value (`of type int`), and it names no held type at all: the sentence speaks for every candidate the comparison was attempted on, and they need not have carried the same type. **The remedy it carried was false, and is corrected here.** "or bind a regex capture, which is always text" stopped being true when #781 made a capture pattern over a non-string argument an authoring finding — the extraction yields `[]` there, so such a capture binds nothing on any trajectory, and an author who took the advice traded one silent never-true check for another. Both live sites go, the sentence itself and `docs/GRADING.md`'s quote of it, replaced by the wording the gate already ships: "extract a regex capture off a field that holds text". **No pack shipped under `examples/` moves** — every binding reference in them binds a regex capture or a text-valued argument and is compared against text, which the table admits. No wire schema, protocol version, `grading.yaml` surface, CLI or published Python API changes, and `undecided` does not move: a reported comparison is still `passed=False, undecided=False`.

- **cli**: **a run that could not grade a trial exits non-zero.** `tolokaforge run` and `tolokaforge worker` returned success whatever the run's completeness, so a CI step gating on the exit code saw a clean run that was missing an arbitrary, model-correlated subset of its grades — the failure mode the entry below is about, reported as a pass. **This is a change to the CLI exit-code contract.** A completed run in which any trial is classified `ungradeable` — the trial was attempted and measured, `trajectory.yaml` carries a `grading_error` and no `grade` — now exits `1`. **The run still completes first**: every artifact is written, the end banner prints, and the run directory is still emitted on stdout, which makes this the first non-zero exit that deliberately prints it. The end banner keeps `success=True`, because its axis answers "did the run execute" and this run did; overloading it would make `✗ Run failed` mean both an aborted run and a complete-but-lossy one. The completeness verdict's channel is the exit code plus one error line naming the count, the first few trial ids and the total. **What triggers it is `ungradeable` alone.** A trial the provider or the substrate killed — `api_timeout`, `rate_limit`, `provision_error`, counted under `infrastructure_aborts` — produces no verdict either and deliberately does **not** trigger the gate: those trials were never measured, they sit outside the measured denominator by design, and failing a several-hour run for one rate limit is how an exit code becomes something operators route around rather than read. A negative lock pins that distinction. **There is no tolerance threshold** — a threshold is a way to re-silence the signal, and the number an operator would tune is already `ungradeable` in `aggregate.json`. **What an operator does about it**: the run directory is complete, so read it — `ungradeable` in `aggregate.json` says how many, `per_task_metrics.json` says where, each named trial's `trajectory.yaml` `grading_error` says why — then rerun those trials or accept the loss deliberately. **The `RUN_DIR=$(tolokaforge run …)` idiom still captures the path**, because the path is emitted before the exit code is decided — but under `set -e`, which is most CI, the shell now aborts at that assignment on a lossy run. That is the intent; a caller that wants the directory anyway writes `|| true` and then decides. `Orchestrator` publishes the same verdict as a frozen `GradingCompleteness` on `orchestrator.grading_completeness`, set by both `run()` and `run_worker()`, since an embedder gets no exit code; it is unbound until a run finishes rather than defaulted, so reading it off an orchestrator that never ran raises instead of reporting a complete run. `Orchestrator.run`'s return type and `run_worker`'s returned dict are unchanged. No counting moves: `total_trials`, `measured_trials`, the `ungradeable` counts and where an ungradeable trial lands in every rate are exactly as before.

- **grading**: **a trial's tool-call id is episode-unique, so a provider that reuses one is gradeable instead of lost.** A provider mints `ToolCall.id` as model output and nothing obliges it to be unique across an episode: `moonshotai/kimi-k3` via OpenRouter names each call `<tool_name>:<index within the turn>`, so calling the same tool at the same position in two turns emits one id twice — across turns, never within one. That id is the only key joining a call to the result it produced, so such a trial could not be graded **at all**. Measured on the issue's own 7-turn, 12-call trajectory: all three of `build_trial_timeline`'s paths refused it — message view + record (`tool-call id 'get_employee:1' was recorded twice, at sequences 1 and 5`), message view alone (`… is answered twice, the second time by the tool result message at index 9`), and records alone. The trial then classified `UNGRADEABLE` and its grade was simply missing from the run. **The rule** is one leaf module, `tolokaforge/core/tool_call_ids.py`: the k-th occurrence (0-based) of a raw id `x` is keyed `x` for k = 0 and `x#<k+1>` thereafter, with the derived key re-checked against the keys already handed out so a provider that emits `x#2` itself still leaves every call a key of its own. It is applied at two points and computed once — the agent loop assigns it at ingestion, between the generation and the assistant message, so all four consumers agree (the assistant message, the tool executor and hence the runner's own record over gRPC, the trial's recorder, and the `role: tool` message's `tool_call_id`); and `build_trial_timeline` derives it per view from that view's own observation order, so a bundle recorded **before** this change re-grades and re-checks from what it already holds, with no rerun. A reassignment is a `logger.warning` naming the tool, the raw id and the assigned one, so the run log says which providers need the disambiguation. **Nothing moves for a provider that already minted unique ids.** The rule is the identity on a repeat-free sequence, so every Anthropic (`toolu_*`) and OpenAI (`call_*`) trial, every fixture and every bundle on disk is byte-unchanged; `tolokaforge retrace` over a corpus of them reports the same verdicts. Where a provider **did** reuse an id, the second occurrence is now written `<id>#2` in `trajectory.yaml`, `tool_log.yaml` and on the timeline — a value that was previously ungradeable, so no recorded grade changes. Nothing anywhere parses a `call_id` (swept: no `split` / `partition` / `startswith` / regex against it), so `#` is inert on gRPC, YAML and JSON alike. **What still refuses**, each naming the offending key and where it occurred: a record whose key matches no declared call, a `role: tool` result whose key matches no declared call, and — new — a record whose `tool_name` disagrees with the declaration its key joined it to, which is the independent corroboration an occurrence-order join needs. The uniqueness check that fired inside the timeline builder is **removed** rather than left unreachable, and the property it asserted is locked by tests on both the message-view and records-only paths. No wire schema, protocol version, `grading.yaml` key, CLI surface or published Python API changes, and no component score moves.

- **grading**: **`state_checks.jsonpaths` says which state each assertion reads: `path:` addresses the trial's database, `path_glob:` the filesystem.** The runner composes its JSONPath state from the trial's database alone — `db` and `tables`, nothing else — so a `path:` rooted `$.filesystem[…]` cannot resolve there, and a `path:` of any other root needs a task whose `initial_state` provisions a database. Both halves were measured: a `$.filesystem`-rooted `path:` on a task that seeds a database scores **1.0 on the core engine and 0.0 on the runner**, and on a task that seeds none, `GradeTrial` returned `Grading error: TrialNotFoundError`. Three shipped reference packs wrote the first shape and could not be graded on either substrate; each is now authored against the contract and grades, with every migrated assertion resolving **1.0 on both substrates** when the file content satisfies it. **Migrating a pack maintained outside this repository is the same two-line edit made here**: an assertion reading a file moves from `path: "$.filesystem['/env/fs/agent-visible/X']"` + `contains:` to `path_glob: "/env/fs/agent-visible/X"` + `contains_ci:`, and an assertion reading the database stays a `path:` on a task that seeds rows. **`contains_ci` is required rather than preferred**: the runner's file-content evaluator reads only that operator and treats an absent one as the empty string, which every file contains, so `path_glob` with any other operator is a runner-side pass asserting nothing while core scores it for real (#466). **`contains_ci` is also a real loosening of the three migrated packs, not only a guard against a vacuous pass**, and it is forced — the runner's file-content evaluator reads no other operator. Measured on the core engine: the `persistent_tools` assertion scores **1.0** against a `CHANGELOG_DRAFT.md` reading `- status: ready`, where the task prompt demands the literal `STATUS: READY` and `contains:` scored **0**; `coding_01`'s scores **1.0** against `return AMOUNT * (1 + TAX_RATE)`, where `contains:` scored **0**. The pairing is case-insensitive on both substrates, which is the price of the one operator both read. **The authoring gate now refuses all three shapes before a trial is scheduled**, each naming the assertion and its remedy: a block reading the database of a task that seeds no tables, a `filesystem`-rooted `path:`, and a `path_glob:` compared with anything but `contains_ci`. Where a caller cannot resolve what a task seeds — any adapter maintained outside this repository — the first is reported unchecked rather than passed silently, and the other two still run. **The gate also reads `state_checks.hash.enabled` the way a run reads it** — through the block's own `StateHashConfig`, at every rule that reads the flag. Measured against the raw key it replaces: `enabled: "false"`, `"no"`, `"off"` and `"0"` are the `false` both substrates grade on, so a gate testing the YAML string's truthiness **refused** a pack that seeds no tables and grades cleanly, and **passed** a hash source declared beneath the same flag that nothing reads. A block the model refuses is still read off the key, so a pack that cannot load is not blessed by the coercion. Measured over the tracked corpus: **40 `path_glob` entries, 0 refused**, and the `tolokaforge validate` reject counts do not move. `GradeTrial` refuses the same shapes at grade time, for the trials the gate never sees. No component score, wire field, protocol version or grading key moves.

- **grading**: **a trial that scored no component says why instead of saying that none was evaluated.** `Grade.reasons` opened with `No grading components evaluated` whenever the runner's renderer produced nothing, and then appended whatever else the grade had to say — so a task declaring no grading at all read `'No grading components evaluated | no component was configured and no weight names one, so nothing was scored and nothing was owed'`, two sentences for one fact, the first of them a claim about the renderer rather than about the trial. With every registered component now narrating itself, that text can only be reached where nothing was scored, and there the fold already explains itself: `resolve_uncounted_fold` returns a verdict only with a non-empty reason, on both of its branches, and `GradeTrial` carries it whenever set. **The four shapes that reach it were each driven through a real `RegisterTrial → GradeTrial`** — a task with no `grading` key, one with an empty block, one whose only rule was skipped because the timeline carried no events, and one whose `combine.weights` names a component the config never asked for — and each keeps its remaining account with the leading sentence dropped. `Grade.reasons` for such a trial is now that account alone. The grade's segments are also **joined once rather than appended in sequence**, without which an empty renderer output would open the string with a separator; the four appends `GradeTrial` made are one list and one join. No component score, wire field, protocol version or `grading.yaml` key moves.

- **grading**: **a trial graded by `custom_checks` says so in `Grade.reasons`, in one sentence both substrates emit.** A custom-checks suite could decide a trial's whole verdict and contribute nothing to the grade's account of it: the runner's renderer enumerated its components by hand and had no branch for this one, so a custom-checks-only pack came back `reasons='No grading components evaluated'` beside `components.custom_checks=0.0` — an assertion of the opposite of what happened. The runner now carries a `Custom checks:` segment, and the core engine's `Custom: 1 passed` counts string becomes the same text, rendered once by `custom_checks_reason` in `core/grading/checks_helpers.py`. **What the sentence says**: the component score, how many checks reached a verdict, and every check that reached one and did not pass — by `check_name` and message, the way `Transcript:` and `Trace check <id>:` already name theirs. A skipped check reached no verdict, so it is counted and not named. A suite that reached no verdict at all says so rather than quoting an aggregate over nothing, and **a suite that could not run names the error it failed with** — the error text is the only thing separating a module that failed to import from a suite whose every check failed, and it now survives on the grade rather than only in the `__executor__` wire entry. That covers every path either substrate can take: the runner's four early returns (no artifacts directory, an executor that reported an error, an executor that raised, a suite that decided nothing) and core's pre-run failures (no checks file, an unreadable declared initial state, a module that would not load, and the absent-task-directory branch its own gate makes unreachable — #996) each build the `CheckResultSet` they would have produced and render through the one function. **One downstream consequence, measured**: `failure_attribution` splits `reasons` on `|` and keeps every segment matching `FAIL` case-insensitively, so a failing custom-checks trial now contributes the `grade_fail_patterns` evidence its transcript- and trace-graded siblings always did. Naming only the losing checks is what keeps that honest — a passing suite's sentence carries no check name, so a check called `no_failures_logged` cannot manufacture the evidence. **A suite that could not start also begins contributing that evidence**, which is correct — the trial did fail and the suite is why — and the sentence says `the suite failed to run` in the renderer's own words rather than inheriting the substring from the error it quotes: the four error texts either substrate can produce disagree with each other (`Failed to load/run checks: …` carries it, `checks file not found: checks.py` does not), so a rule reading the quoted text would answer one state four different ways. **A custom-checks-graded trial's `reasons` therefore changes on both substrates**, and lands in every `grade.yaml`; no wire schema, protocol version, `grading.yaml` key, CLI or component score moves.

- **grading**: **`CheckResultSet.decided` is public** — the results a check suite reached a verdict on, which is every one that did not skip. It is the single predicate behind `decided_something`, `aggregate_score` and the sentence `Grade.reasons` carries for the component, and it is shared rather than copied for a reason worth knowing before reading any of the three: a second reading of "reached a verdict" would let a narrowing move the component's score without moving what the grade says about it, so the fold would report the component unscored while the sentence named a check that failed. A task's `checks.py` can read it off the `CheckResultSet` a `CheckRunner` returns. Additive — no existing member changes.

- **grading**: **`tolokaforge validate` checks a binding reference on an `args` predicate against both declared types, where it exempted every such reference wholesale.** Two arguments correlate natively only where the tools type them so: `read_file.path` compared against a binding off `read_file.offset` is false on every trajectory, and so is the reverse — which this tier exempted wholesale, so the author paid for a trial and read their own type mistake as the agent's failure. The gate is the tier that answers before the run is paid for; the evaluation-time backstop reports the same pair after the fact, and only over the residue no schema types. Whether a pair can ever hold is a per-operator table rather than "do the two types differ": `equals_binding` holds across `integer` / `number` / `boolean` and between two arrays or two objects, while `contains_binding` finds any scalar inside an array or object haystack by descent and finds a container inside nothing. An extraction no schema describes still has a type — `tool`, `text` and `result` are text, a bare `field: args` is the argument mapping, and a `pattern` binds a capture, which is text. This is the one rule resting on **two** schemas' claims, so the weaker decides: an error only where both forbid extra arguments, an advisory wherever either permits them. It stands down where another rule already reports the same gap — a predicate carrying a `regex` beside its reference is left to the textual rule, and a matcher naming no one tool or a path below its first segment keeps its single existing `unchecked` line. An argument whose schema writes no `type`, or one outside the six JSON type names, is `unchecked` rather than refused. **No pack in this repository moves**: `validate` over the example corpus is 29 valid, 0 invalid, and all 7 binding references between the four packs declaring a binder sit on types that correlate. A pack maintained outside this repository correlating two differently-typed arguments moves from a trial that was paid for and scored the constraint against the agent to a refusal before the first token. No grading verdict, wire schema, protocol version, `grading.yaml` surface, CLI or published Python API changes.

- **grading**: **`tolokaforge validate` refuses a `bind.values[*].pattern` over an argument the tool's schema types as something other than a string.** A capture is taken off text alone: the evaluator narrows a value by pattern only where the value is a `str` and yields nothing otherwise, so a pattern over an `integer` / `number` / `boolean` / `array` / `object` argument — or over a bare `field: args`, which binds the argument mapping itself — binds the name on no event whatever the agent did, and the default `on_unbound` reports that as the agent's failure with the message a genuine miss carries. The finding is reported at the extraction's `pattern` key, which is the key the author deletes to fix it, and it does not depend on which predicate reads the name or on whether any does. It also does not depend on whether the pattern compiles: an uncompilable pattern over an `integer` argument now draws two findings at that one key, because fixing the regex does not make an integer capturable and an author shown only the compile error would repair it and still bind nothing. Severity rests on the binder's tool: an error where its schema forbids extra arguments, an advisory where it permits them. **No pack in this repository moves** — the only pattern-bearing extraction among them is over a `string`. A pack maintained outside this repository writing a capture over a non-string argument moves from a paid-for trial whose binding silently yielded nothing to a refusal before the first token. The remedy the type rule names is correspondingly narrowed, and every rule that names the same repair now names it identically: "Correlate two arguments the tools type the same way, or compare a regex capture against a field holding text" — both halves conditional, and both conditions checked by the entries above. No grading verdict, wire schema, protocol version, `grading.yaml` surface, CLI or published Python API changes.

- **grading**: **`tolokaforge validate` refuses a binding reference read by a `status` or `executor` predicate when the tool's schema types the extraction as non-string.** The gate already reported that shape for `tool`, `text` and `result`: a value bound out of an `integer` argument is never a substring of text and equals nothing a text field holds, so the check is false on every trajectory and reads as the agent's failure. It exempted `status` and `executor` on the strength of a hand-written constant claiming those were not among the event's string fields. They are — both are closed vocabularies declared as `str` subclasses, so the value a predicate on them compares *is* text — and the identical never-true check went unreported. The set is now computed off the event's own resolved annotations rather than listed by hand, so the membership and the claim behind it cannot disagree. Severity is unchanged and still rests on the binder's tool alone: an error where that schema forbids extra arguments, an advisory where it permits them. **No pack in this repository moves** — none carries a binding reference on either field. A pack maintained outside this repository writing `status: {equals_binding: n}` or `executor: {equals_binding: n}` over an extraction its schema types `integer` / `number` / `boolean` / `array` / `object` moves from a paid-for trial that reported the failed comparison at grade time to a refusal before the first token. Two ways to write the intent stay legal, each with its own condition: reference the binding from an `args` predicate whose argument the tool types so that the two can ever compare, or bind a regex capture off a field that holds text. Both conditions are themselves checked — see the two entries above, which ship in this same release. No grading verdict, wire schema, protocol version, `grading.yaml` surface, CLI or published Python API changes.

- **docs**: **the runner-engine version lock is one table under its own heading, and every key on it names the release whose image first presents it.** The list lived as a bold lead-in on a prose line with no heading of any level around it, so nothing could address it and both mirrors' anchors resolved to the enclosing section instead; it is now `### Runner-engine version lock` in [`docs/GRADING.md`](docs/GRADING.md), and `docs/RUNNER.md` and `docs/TROUBLESHOOTING.md` point at it rather than re-enumerating a subset each. **Six of the nine entries were stale**: they said a key was declared in "this release" for keys whose images have presented them since `v0.13.1`–`v0.16.1`, and two paragraphs elsewhere in the same file made the same claim outside the list entirely. Each row now states the release itself, so the claim stops rotting every time the engine ships. `first declared by` means the release whose image first presents the key **in the shape that row's lock is about**, which is why `state_checks.id_fields` reads `v0.16.1` (the list-valued key) and `combine_method` reads `v0.13.1` (the current value domain) rather than the release each name first appeared in. Membership follows a stated floor — the table speaks about images from `v0.13.1` onward — instead of habit, and two keys join it under that rule: `transcript_rules.tool_expectations`, which carried a version-lock paragraph while not being on the list at all, and `custom_checks`, which had neither. `custom_checks` is the one the omission cost most: the engine emits it for **every** pack, so an image that predates it rejects every trial spec, and the list that answers "which keys bite" did not name it. Membership is now held rather than listed by hand: **every** grading key the engine can put on the wire is either on the table or recorded as predating the floor, so a field added below any container — the way `hash_weight` itself arrived — has to join the table or be argued into that record, and an omission fails a test instead of reaching a reader. The count words in all three documents are gone; each row states its own breadth, which is now checked against what the models actually emit rather than written by hand. **Documentation only** — no wire schema, protocol version, `grading.yaml` surface, CLI or published Python API changes, and no key's behaviour moves.

- **grading**: **a failed tool call records the tool's own failure text on both grading substrates, and a `trace_checks` matcher may now read it.** A `result` predicate — and a binder extracting `field: result` — loads beside any `status` predicate or none, where both were rejected at load unless paired with `status: {equals: success}`. A pack can now assert *why* a call failed (`{status: {equals: error}, result: {contains: already refunded}}`), not merely that it did. Loosening only: every block that loaded before still loads and grades identically, since the lifted rule refused configs and never scored one. The in-process executor and the gRPC runner recorded three different wordings for one underlying failure — `Tool execution failed: <msg>` core-side, `Tool error: <Type>: <msg>` runner-side, and `Tool '<name>' not found` against `Tool '<name>' not found in agent tools` for an unknown tool — so a `result` predicate over a failed call read whichever substrate happened to run the trial. One failure now has one text on both: the message the tool signalled in `ToolResult.error`; the message a raised exception carries, or its class name where it carries none; `Tool returned failure with no error message` where a tool failed without saying why; and `Tool '<name>' not found` for a call naming a tool the trial does not have. `ToolExecutionError`'s `str` is its `message` alone (`tool_name` stays an attribute), and the exception's type and traceback stay in the executing layer's log on both substrates. **Agent-visible**: the `role: tool` message a model reads mid-trial is the same text behind an `Error: ` prefix, so it is terser than before, and a failure with no message of its own reads as the sentence above rather than `Error: None`. **Task packs**: no recorded bundle in this repository quotes any of the retired wordings and no verdict moves — but an external pack whose judge rubric or trace check quotes `Tool error: <Type>: …`, `Tool execution failed: …` or `… not found in agent tools` must target the tool's own text instead. No wire schema, protocol version or task-contract change.

- **grading**: **`transcript_rules.tool_expectations` grades the agent's own tool calls.** A `required_tools` entry is satisfied only by an agent call that succeeded, and a `disallowed_tools` entry is violated only by an agent call, at any status — a user-simulator call to the same tool counts for neither list, the posture the phrase rules already take towards the user's text. Both sub-checks counted every executed call whatever the record's `executor` said, which left the rule family divided against itself: the sibling `required_actions` has always matched its declared `requestor` against that same field. **No task's grade moves.** `executor: user` is unreachable in every run today, because no code path constructs a user-side tool executor (#688), so no timeline a run or a committed fixture produces carries a call this filter excludes — measured across the unit and canonical corpora, which move zero rows. The contract is pinned ahead of that activation, so wiring the user-side executor cannot quietly switch these two checks to an any-actor reading. Where the actor is the point, `trace_checks` matchers carry an explicit `executor` field and are the vocabulary for "no actor may call `x`" or for an assertion about a user-side call. The authoring surface is unchanged: `tool_expectations` still declares exactly `required_tools` and `disallowed_tools`, and no wire or engine-version lock moves.

- **grading**: **a custom check in a pack that declares `initial_state.json_db` as a path receives the state the task starts in, where it received `{}`.** The host's check-context call site took the authored `json_db` only when it already was a mapping, so a pack writing its seed state inline and a pack writing the identical state in a file beside `task.yaml` handed their checks different evidence: the path pack's check graded as though the task started empty, and nothing on the grade said so. Both shapes now read through the same core reader `state_checks.hash.expect_initial_state` reads its state with — an inline mapping used as it stands, a path resolved under the task directory and loaded. **This is a pack-facing change**: a check branching on `ctx.initial_state.data` being empty — which a check written against a path-declared pack may well do, having never seen it populated — will now see the state. **A declared path that does not resolve fails the `custom_checks` component** at `0.0` with `context build error: …` naming `initial_state.json_db`, the path as the task wrote it and the problem, instead of silently handing the check `{}` and scoring whatever it made of that. That refusal is a **backstop, and it fires post-trial**: a live native run already refuses the same pack before the trial is paid for, when the adapter builds the task description, and `tolokaforge validate` refuses it with no run at all — what reaches the backstop is host-side grading that runs no trial, which is tests, replay and re-grading flows, and direct `GradingEngine` callers. **An empty declared state is a different fact and is not refused**: an inline `{}` and a file holding `{}` both reach the check as `{}`, the same evidence a task declaring no `initial_state.json_db` at all supplies. The empty-state refusal belongs to the hash source alone, where it exists because an empty state hashes to a digest no trial can match; a check reads the state as evidence and can decide about an empty one itself. **No in-repo verdict moves**: the only pack combining custom checks with a path-declared `json_db` whose checks read `initial_state` is `tests/data/projects/food_delivery_2/tasks/order_modify_with_checks`, whose `get_order` helper indexes an `"agent"`-nested shape the declared file does not carry — re-measured at this tree, its nine checks report the identical statuses and the identical `0.25` component with the state resolved and without it. `build_check_context` is untouched and still performs no I/O; resolution is the call site's, which is what keeps that helper shared by both grading paths. The host hands a check the raw authored mapping while the runner hands a normalised `{table: [records]}` view of the same declaration — that divergence is tracked in #972. Closes #916.

- **grading**: **a task declaring a `grading:` path with no file at it is refused before the trial, not while the paid-for trial's artifacts are written.** `grading_source_under_adapter` stats the path the task names rather than taking the join as a source: a path with a file behind it is the source it always was, and a path with none is the same absence as naming no source at all — refused under `adapter_type: native`, which grades from that file, and reported `unchecked` under any other adapter, which resolves its own grading config. All four surfaces inherit the ruling from the resolver with no routing change of their own. **Exit-code movement**: `tolokaforge validate` over such a pack under the native adapter moves from `0` to `1`; a `run` or `prepare` of one is refused in the pre-flight, where it previously passed the gate, scheduled the trial and died in `Conductor._write_artifacts` with every token already spent; and the rubric-migration reconciler refuses it naming the pack instead of reading a declaration off a path nothing wrote. The refusal names the task, the ref it wrote, the path that ref resolved to, and the two ways out — correct the `grading:` path, or create that file. **No corpus moves**: all 132 tracked `task.yaml` files were re-measured at this tree and none declares a grading path that is not on disk.

- **runner**: **model-to-table registration reads the task's declared `state_checks.id_fields` map instead of the source text of each model's `get_id()`.** `ToolFactory._register_toolset_models` parsed `inspect.getsource(model_cls.get_id)` for a `return self.<field>` line and then required that field to be the **first** key of the table's first seeded record — the runtime source-reading `id_fields` exists to retire, wearing a positional guess on top. A table is matched to a model by the key it *declares*: every component of that key (a table absent from the map defaults to `"id"`) must be a field of the model and present in the table's first seeded record — **membership, not position** — and that record must still validate against the model, the same gate that has always discriminated two tables sharing a key shape. **(a) Composite and non-first-position declared keys register eagerly** where all of them previously fell through to a `Cannot match model …` warning: a model whose `get_id` returns an f-string over `[account_id, symbol]` yielded no field name at all, a model whose declared key sits second in the seeded record failed the positional check, and a model whose source is not on disk raised into the same skip. **(b) One shape moves the other way**: a *read-only*, non-`"id"`-keyed, seeded table with **no** `id_fields` entry — write-refused already, so read-only by construction — is no longer registered eagerly and resolves through the DB proxy's class-name fallback on first use, landing on the same table wherever that fallback's suffix match reaches it. Declaring `id_fields` for it — one YAML line, already mandatory for any table the pack writes — restores eager registration. **(c) A declaration plus passing validation outranks an undeclared default**: a model keyed `id` that also carries a declared table's key as an ordinary foreign-key field registers to the *declared* table **iff** that table's seeded records validate against it; where they do not — the ordinary case, since a table keyed `review_id` rarely validates as a model keyed `id` — the claim stays where it is today. More generally, wherever the parse-and-first-position selection and the declared map disagree, the declaration plus passing validation wins, including for the newly-eagerized composite models. Every other matching strategy is unchanged — `table_name` ClassVar first, suffix matching on empty tables after, report-and-skip last — as are the strategy order, the claim-once table discipline and the absence of any raise for a model that matches nothing. Closes #922.

- **runner**: **`get_by_id` builds its JSONPath filter from the JSON type of each key value, so a key that is not a string — or a composite key any component of which is not a string — resolves in one query instead of degrading to a full table scan nobody could see happening.** `DBServiceProxy.get_by_id` interpolated the value into a single-quoted string literal, so `get_by_id(Order, 5)` asked for `$.orders[?(@.id=='5')]`, matched nothing, and fell through to `get_all()` over the whole table — two round trips where one suffices, on the existence check `create()`, `update()` and `delete()` run for every record they touch — while a key carrying a quote (`O'Brien`) closed the literal early, drew an HTTP 400, and had it swallowed by a blanket `except` into a `logger.debug` line before the same scan absorbed it. The composite path spelled its predicate the same way, one quoted component at a time, so a table keyed `[account_id, slot]` full-scanned every lookup the moment one component held a number. A value is now encoded by type: `bool` as `true`/`false` — checked ahead of `int`, which it subclasses — `int` as a bare decimal, a finite `float` as its bare `repr`, and `str` single-quoted with `\` and `'` escaped. One encoder serves both paths, and the composite conjunction mixes typed literals freely (`@.account_id=='A1' & @.slot==2`). **A value with no literal in this dialect is not queried at all**: `None` (neither `==null` nor `==None` matches a stored `null`), a non-finite float, a float whose `repr` needs exponent notation (`1e+20` is a parse error, not a number token), and any container. It scans directly, and says so at `info` naming the table, the field and the value's type — and because a conjunction is only as askable as its weakest component, **one unencodable component skips the whole composite query** rather than asking a narrower question than the caller posed.
  **A hit is a candidate, not an answer.** The dialect compares numerically by coercing the stored value — `@.id==7` matches a stored `"7"`, `@.id==0` matches a stored `0.1`, `@.id==true` matches a stored `1`, and `@.account_id==1` drags in a component stored as `"1"` — so a hit counts only once the validated model satisfies **the predicate that path's own fallback scan uses**: `get_id(item) == value` for a single-field key, and component-wise equality against the model's own dump for a composite one, which is what its scan has always compared. Query and scan therefore answer with one definition of equality on each path, and every kind of miss falls to the scan, which means the query can save a round trip but never change an answer. **One behaviour tightens**: a lookup whose Python type disagrees with the model's key type — `get_by_id(StrKeyed, 7)` against a row `{"id": "7"}` — returned that row through the old quote interpolation and now returns `None`. A type-consistent pack cannot observe it, because Pydantic's lax validation folds a stored `"5"` into an int-typed model, so seed data written as string digits still resolves in the one query. A hit the model cannot validate at all is likewise a miss rather than a query failure; the record still surfaces, raised by the scan that reads the same row and is the authority on it.
  **The log stream now distinguishes the four reasons a lookup scans**, on both paths alike, where all of them previously read as one `debug` line or none: a value with no literal at `info`, a failed query at `warning` naming the key, the target and the error — never `debug` again — and the two misses at `debug`, because `create()` asks this question about every record it is about to insert and legitimately expects "not there". The two are worded apart: a declared key that matched nothing, and a table with **no usable key at all** — no `state_checks.id_fields` entry and a model with no `id` field, the shape the write path already refuses — which no longer issues the guaranteed-futile `@.id` query before scanning. Each message names its target the same way, a single-field key as `<table>.<field>` and a composite one as `<table>.<field>+<field>`. The per-lookup `info` chatter that would have drowned those lines (model-key-to-table resolution, model registration, and `get_all` dumping the registered-table list on every scan) is `debug`. The db-service defect underneath one of them is shielded, not fixed: a numeric filter over a column holding `null` raises through that service's blanket `except` as a 400, so the same query is valid on one dataset and an error on another (#966) — the proxy now reports it and still returns the right row. No task-pack, config or wire schema changes. Closes #921.

- **db-service**: **an upsert whose record omits its resolved key field is refused, and every upsert refusal is atomic over its batch.** `PATCH /trials/{id}/state/{table}` matched an upsert's target row with an absent-tolerant `.get` on both sides, so a record missing its single-field key (default `id`) projected `None` and matched any stored row also lacking the field: two keyless upserts against a seeded table each returned 200, and the second silently overwrote the first. A record that does not **contain** the resolved key field now draws HTTP 400 naming the table, the key field, the record's keys, and the zero-based operation index — the same shape as the composite refusal, whose `details` gain `op_index` too. Every upsert in a batch — including one carrying no `record` at all — is validated **before any operation applies**, so a batch refused over an upsert leaves rows, version, and the SQL mirror untouched, where a mid-batch refusal previously left earlier operations applied with the version un-bumped and the mirror stale (the same non-atomicity behind the non-upsert 400s is #962's). The matcher now requires the stored row to **carry** every key field, so an explicit `null` key value — legal on the single-field path, mirroring the diff layer's key resolution — updates the row that genuinely stores `null` instead of merging into the first row lacking the field. **Previously-succeeding keyless upserts now 400**: nothing in-tree can emit one — the diff layer validates key presence upstream and the runner's DB proxy serializes full models, keeping `None`-valued fields present — so this is additive safety, not an upgrade-together migration; a direct HTTP writer must include the key field in each upserted record. Live stacks pick the refusal up at the next `make docker-build-core`. Closes #920.

- **grading**: **`tolokaforge validate` holds a pack's `state_checks.id_fields` declaration against the state that pack seeds, so a key that addresses no seeded record is a `✗` line instead of a clean bill of health followed by a refused run.** The cross-check existed only where a task description was built — the pre-run gradeability gate and `RegisterTrial` — and `validate` builds none, so a pack seeding `account_id` / `symbol` while declaring `id_fields: {positions: [account_id, ticker]}` exited 0 with `1 valid, 0 invalid` and was refused later, naming `['ticker']`, once a run had been started. **`validate` now fails packs it previously passed**, on all three findings the run path already refused: a declared table absent from `initial_state`, a declared key component absent from every seeded record of its table, and a declared key that does not uniquely identify those records. The migration is each finding's own remediation text — fix the typo, add the table, seed the field, widen the key to a composite list, or set `state_checks.relaxed_validation: true`. No in-repo pack moves. **The verdicts of the pre-run gate and of `RegisterTrial` are unchanged**: one computation now backs all three gates, so a pack cannot be refused at one and passed at another, and a differential test drives one defective pack through the adapter and through `validate` asserting the identical sentence. `relaxed_validation` downgrades identically at `validate` — a logged warning, no report entry on any channel, never fatal — which is what it already did on the run path. A task whose seeded state this command cannot read draws an honest `?` line for `state_checks.id_fields` rather than a false refusal or a silent pass: `initial_state.json_db` is the native reading, and a task owned by an adapter maintained outside this repository may seed its state some other way, with `RegisterTrial` still enforcing at run time. **One sibling blind spot closes with it**: a native task whose `json_db` names a file that is not on disk also validated clean and failed at run start with `JSON DB file not found`; reading the seeded state at validate makes it a `✗` naming the path. No new public Python API. Closes #923.

- **grading**: **an adapter answers what supplies a hash source beneath a task's authored `state_checks.hash` block, so a pack whose source has been lost is refused before any trial is paid for instead of reported as unchecked.** A bare `hash: {enabled: true, weight: 1.0}` — the convention for packs whose golden-actions fixture the authored block never names — was routed to the never-fatal `unchecked` channel for every non-native `adapter_type`, so a pack with that block and **no fixture anywhere** passed `tolokaforge validate` with exit 0 and one `?` line, then lost the trial at grade time after the tokens were spent. `BaseAdapter.grading_hash_source_layer(task, task_dir)` is the new **published, additive** hook that closes it: a **classmethod**, deliberately unlike the instance-based `grading_combine_layer()`, because `tolokaforge validate` constructs no adapters and must keep validating packs whose adapter is not installed. It reports facts — the source the adapter computes the comparison from, and whether it is `USABLE`, `MISSING` or `EMPTY` — and the gates decide fatality. **An adapter that overrides nothing inherits `unresolvable()`, which is exactly the previous behaviour**, so adapters maintained outside this repository need no change to keep today's outcome; `NativeAdapter` answers "nothing beneath the block", which is what keeps a native pack's sourceless enabled hash the refusable authoring defect it already was. Both gates consult it: `tolokaforge validate` asks the registered class, the pre-run pre-flight asks the adapter instance it is about to grade with. **The behavioural change is for packs whose adapter implements the hook**: a usable source turns the bare block from an `unchecked` line into a checked pass, and a missing or empty one becomes a fatal finding at `state_checks.hash.enabled` naming the fixture in the adapter's own vocabulary, at `validate` and at pre-run alike. **`tolokaforge validate` now triggers adapter entry-point discovery for the first time**, so a *broken installed* adapter plugin logs a load warning during validation — it never raises, and an environment without the plugin installed is unaffected. Two registry fixes ride along, both load-bearing for that resolution: `_ensure_adapters_discovered` read the registry's own non-emptiness as its "already discovered" flag, so a single `register_adapter()` before first use suppressed entry-point discovery for the life of the process and left `native` unresolvable; discovery now tracks its own flag and **merges into** the registry rather than replacing it, so a registration written before discovery survives it and wins a name collision against a discovered entry-point. `adapter_class(name)` is exported for resolving a name to its class, answering `None` — never raising — for an adapter that is unknown or failed to load. Closes #940.

- **search**: **the TypeSense address is a property of the running stack, injected once when the runner container is created — not a per-task fact rewritten after start-up.** The runner container is created with `TYPESENSE_HOST` / `TYPESENSE_PORT` naming the bridged server's network alias (`typesense:8108`), and the API key travels only inside the `TOLOKAFORGE_SECRETS_JSON` payload: it is registered with the `SecretManager` before any log record could carry it, which also closes a measured leak — the auto-generated key was outside the redaction set and was logged verbatim in the adapter-params line. The post-start rewrite of `orchestrator.typesense` and the description-cache invalidation that had to ride with it are deleted; the Docker bridge only connects the TypeSense container to `runner-net`. The runner resolves its address once per registration and reports which source answered — the stack's variables win, and a task's own `search.host` / `port` / `api_key` serve only the runs where no stack set them (`auto_start_services: false`, worker mode, adapters that still emit an address). Those three `SearchConfig` fields remain **accepted** during the transition and are retired by #951 once no adapter emits them. **`search.plane` declares which plane serves a task's corpus** (`typesense` or `rag_service`; a task declaring neither has it derived from the connection details it carries). The engine emits it on every task, so **a new engine against an older runner image rejects every trial at `RegisterTrial` — upgrade engine and runner image together** (`make docker-build-core`); an older engine against a current image is unaffected, since the field defaults to unset. **Two run-config shapes that previously completed with a dead search plane now abort before any trial**, each naming the address, why the runner cannot reach it, and the remedy for the mode: `mode: local` with both a pinned `port` and a pinned `api_key` (that shape skips the managed start, so nothing is bridged and the loopback `host` default would be injected verbatim — inside the runner container a loopback address is the runner itself), and `mode: remote` with `host` left at its `127.0.0.1` default. Closes #927.

- **search**: **a declared search plane that cannot be made to work stops the run or the trial, instead of completing and scoring trials whose search tool never worked.** Three fail-soft sites chained. `_ensure_typesense_started` discarded `TypeSenseServerManager.start()`'s boolean, so a server that never became ready still had its would-be port and API key written into `orchestrator.typesense` and still logged "TypeSense server started". `_connect_typesense_to_runner_network` then found no container, warned, and left the host-side address in the trial-facing config without rewriting the adapter's params — inside the runner container that address is the runner itself (#925). And `RegisterTrial` warned on every runner-side failure and reported `success=true`, so a run completed with every `search_policy` call dead and the scores graded as measured agent behaviour. **Run-level failures now abort the run before any trial**: a server that does not become ready within `timeout`, an unavailable Docker foundation layer, or a bridge that cannot be built — no TypeSense container, no `runner-net`, an adapter that cannot carry the rewritten address, or any Docker SDK failure, which now propagates with its own type and traceback rather than being swallowed. Each names the address tried and the reason. **`RegisterTrial`'s error semantics move with it**: a trial that a current runner image registers successfully — a knowledge-base task in a TypeSense-enabled run whose client is unusable — is refused by an image built from this release, so a run that previously "succeeded" with corrupt knowledge-base scores now fails. `Conductor._setup_trial` turns the refusal into a trial failure before the agent loop, so it costs zero paid turns. Seven paths refuse, in three classes: **plane broken** — the runner image cannot provide a search client, the registry returns no client, the registry raises, or it returns a client reporting the server as unavailable; **corpus broken** — a `docindex/*.md` file cannot be read, so the collection name would not match the host-side index, or the task declares a knowledge base and no readable `*.md` arrived in its artifacts; **declaration and bundle disagree** — a `docindex/` corpus arrived for a task declaring no `documents_path` in a run that configured a plane, which the gate would otherwise pass over, registering the trial with no search client behind its `search_policy` tool. Every message names the trial, the domain and the address tried. **No wire field changes** — no `.proto` edit and no `ENGINE_PROTOCOL_VERSION` bump, only the conditions under which `RegisterTrialResponse.error` is populated. The registration gate becomes the conjunction of a run-level half (`search.host` is set) and a task-level half (`search.documents_path` is set), measurably equivalent to the previous `host`-only gate across every adapter shape checked: a task with no knowledge base does no TypeSense work, and neither does a knowledge-base task in a TypeSense-disabled run — which is now every such run, because the orchestrator emits the connection details only for a plane that is *effectively* enabled: `mode: disabled` stops them as `enabled: false` always did, where previously `mode: disabled` still handed every task an address nothing answers on. `mode: remote` keeps emitting. One precedence shifts — a task declaring `enabled: true` alongside a broken TypeSense plane now reports the TypeSense error rather than the RAG one. Closes #926.

- **grading**: **the core engine scores `transcript_rules` through the evaluator the runner already used, and its verdicts move.** `GradingEngine.grade_trajectory` folded the block as the mean of four always-present rule buckets, gated by the activity floor and multiplied by two separate `required_actions` / `communicate_info` evaluators; it now calls `evaluate_transcript_rules`, which scores the fraction of the sub-checks the pack actually declared. **A single-rule pack whose rule is violated scores `0.0` where it scored `0.75`; a pack declaring an empty block scores `1.0`.** Measured over the in-repo `grading_parity` corpus, the violating trial of `transcript_rules_disallow_regex`, `transcript_rules_max_turns`, `transcript_rules_must_contain` and `transcript_rules_tool_expectations` each move `0.75` → `0.0`; the other three transcript packs already scored `0.0`. Six semantics change with it, each adopting what the runner — and therefore every docker-graded run — already did:
  - **A phrase rule (`must_contain`, `disallow_regex`, `communicate_info`) reads assistant turns only.** It no longer searches the user's turns, so a phrase the user supplied cannot satisfy a rule about what the agent said, and it no longer searches what a tool returned. A `disallow_regex` pattern aimed at prohibited content that appears only in a tool result stops matching; `trace_checks` result predicates are where that assertion now lives. This removes the family's only ability to read tool output, and with it the three tests that were its locks — `test_a_tool_result_is_searchable`, `test_a_tool_result_is_searchable_on_a_re_graded_bundle` and `test_a_pattern_a_tool_returned_violates_on_a_re_graded_bundle`, the last of which was the repository's only lock on tool-result regex matching.
  - **`must_contain` matches case-insensitively**, as `disallow_regex` and `communicate_info` already did on both substrates. A pack relying on exact case now passes trials it previously failed.
  - **A `communicate_info` entry is satisfied only by the phrase as written**, case-insensitively — the same match `must_contain` makes. A paraphrase that scatters the phrase's words through an assistant turn no longer counts: the bag-of-keywords fallback that accepted a message carrying every word of the phrase longer than two characters and outside a small stop-word list, in any order and any distance apart, is gone. A pack whose `info` string reads as a sentence rather than as a quotable phrase is the one most likely to move, and it moves toward failing.
  - **A `required_tools` entry needs a call that succeeded.** An errored call no longer satisfies it. `disallowed_tools` is unchanged: a forbidden call violates at any status.
  - **A `required_actions` entry reads the tool-call record**, and a trial carrying none fails the check naming that absence rather than reading the message view as proof the call ran. Re-grading a bundle recorded before the `tool_log.yaml` sidecar therefore fails every declared required action; every bundle under `tests/data/` is in that state (#901).
  - **A trial whose timeline carries no events no longer scores `transcript_rules` at `0.0`.** With neither a conversational turn nor a tool call, and no `min_assistant_turns` floor declared, every rule the pack wrote would score against evidence the trial does not carry, so the component is left out of the combine as unevaluated. A declared floor *is* still evaluated on that timeline — alone, without the rules it was declared beside — because no events is precisely the answer the floor asks for.

- **grading**: **`transcript_rules` is one config model, and its wire spelling of a required action's tool is `name`.** The engine-side and runner-side twins are merged: `TranscriptRulesConfig`, `RequiredAction` and `CommunicateInfo` are declared once (`tolokaforge/runner/models.py`) and re-exported by `tolokaforge.core.models`, as `TraceChecksConfig` already was. **No `grading.yaml` edit is required**: the authored key has always been `name:` and is unchanged, and no in-repo pack migrates — measured across 252 `grading.yaml` / `task.yaml` / `project.yaml` files under `tests/data` and `examples`, carrying 41 `required_actions` elements (every one exactly `{action_id, requestor, name, arguments, compare_args}`) and 16 `communicate_info` elements (every one exactly `{info, required}`). What changes is the **trial spec on the wire**, which spelled the field `tool_name`: `transcript_rules.required_actions[*].name` joins the keys under *Runner-engine version lock* in [`docs/GRADING.md`](docs/GRADING.md#runner-engine-version-lock), and unlike most of them it bites in **both** directions — a new engine emits `name`, which an older runner image does not declare, and an older engine emits `tool_name`, which a current image does not. It bites only on a pack that declares `required_actions`. `initialization_actions` deliberately keeps its wire `tool_name`: it is a harness setup instruction rather than a grading assertion about the agent, and its author-side spelling (`func_name`) differs from both.

- **grading**: **a `required_actions` or `communicate_info` element now refuses a key it does not declare, naming the element and the accepted set.** `RequiredAction` was `extra="ignore"` where every sibling grading model is `extra="forbid"`, so a `compare_arg` (singular) typo was accepted silently and `compare_args` resolved to `None` — "compare **every** declared argument" — making the check strictly harder than its author wrote it and failing trials that satisfied what they wrote. `CommunicateInfo` had the same policy and the same exposure through `required`. Both are `extra="forbid"`, `communicate_info` is a list of the typed model rather than of raw dicts, and `tolokaforge validate` names the offending key with the element's index (`transcript_rules.required_actions[0]`), its closest declared field and that element's whole accepted set — the message the block's own keys already got. Two author-visible tightenings ride along, both from `NativeAdapter.to_task_description` validating the block instead of copying it field by field: a `required_actions` element omitting `name`, `action_id` or `requestor` now fails at load rather than translating to `""` / `"user"`, and a `communicate_info` entry carrying a stray key fails at `RegisterTrial` rather than crossing as data. Closes #900.

- **grading**: **`state_checks.hash` is a closed block, and a key it does not declare is refused at load.** The block was `dict[str, Any]`, read member by member with `.get()`, so a misspelled key inside it was dropped without a word: `enalbed: true` beside `expected_state_hsah` over a passing assertion validated cleanly and graded the trial **1.0, PASS** where the correct spelling graded 0.5 and failed, and the runner's own flattened field names (`hash_enabled` / `expected_hash` / `hash_weight`) written into `grading.yaml` did the same while reporting "All checks passed". It is now a Pydantic model with `extra="forbid"` accepting exactly **`enabled`, `expected_state_hash`, `golden_actions`, `weight` and `description`** — `description` among them because packs already write it, and it is read into the hash verdict's reason rather than declared and ignored. **This is a task-pack surface**: a pack whose `hash` block carries any other key stops loading, on every path that reads the block — `tolokaforge validate`, `NativeAdapter.get_grading_config` and `NativeAdapter.to_task_description` — and the gate's callers get the addressed refusal naming the grading file, the closest declared field and that accepted set. The migration is to fix or drop the key; no in-repo pack carries one. What a correctly-spelled block scores is unchanged, and so is what `weight` accepts (`true`, `"0.6"` and `2.0` are still refused by their domain rule). One refusal order moves: a block declaring `db_probes` beside another state source *and* an out-of-domain `weight` now reports the weight message rather than the probe-conflict one, which is what the runner already reported. `golden_actions` keeps its elements unclaimed (#907). The addressed refusal is registered for a block a field holds whole rather than for `hash` alone, so **`transcript_rules.tool_expectations` draws it too**: a `required_toolz` typo names the grading file, the closest declared field and `required_tools, disallowed_tools`, where it previously surfaced as the model's bare `Extra inputs are not permitted`. Closes #730.

- **grading**: **a refusal task declares `state_checks.hash.expect_initial_state: true`, and `expected_state_hash` is retired.** The two substrates hash the same state to different digests — core's `state_digest` folds it with `consistent_hash(to_hashable(...))`, the runner's evaluator with db-service's `compute_stable_hash` — while agreeing on *which* states are equal, so they label every equivalence class differently and a stored digest is readable in exactly one of the two algebras (#915). Measured on the three recorded `tau_retail_mini` bundles, whose declared literal reproduces exactly as core's hash of their recorded final state: **that literal scores `1.0` on the core engine and `0.0` through the runner's**, which is why no wire ever carried it to the runner and why "make the runner honour the stored hash" was not an available fix. `expect_initial_state` names the state rather than a digest — the expected final state *is* the state the task started in — and each substrate computes **both** sides of that comparison itself: core hashes the pack's declared `initial_state.json_db` in either shape an author writes it (an inline mapping, or a path resolved under the task directory) and raises naming that key where the task declares none; the runner resets db-service and hashes what it restored. It is refused beside `golden_actions`, which names a different expected state, and it counts as a declared source, so an `enabled` block carrying only it does not draw the sourceless-hash refusal. **This is a task-pack surface and the migration is real**: a `grading.yaml` whose `hash` block carries a populated `expected_state_hash` stops loading at every read a pack passes through — `tolokaforge validate`, `NativeAdapter.get_grading_config` and `NativeAdapter.to_task_description` — with one message naming both replacements, `golden_actions: [...]` for a task that changes state and `expect_initial_state: true` for a refusal task. The block itself *drops* the key whatever its value rather than refusing it, so a trial bundle serialized against the old schema still loads and re-grades: the substrate that graded every such bundle never read the literal, so dropping it changes nothing they replay, while an author — who can act — meets the raise. No in-repo pack declares it. The value also stops crossing the wire: `RunnerStateChecksConfig.expected_hash` is deleted, and `GradeTrialRequest.precomputed_expected_hash` is gone with `reserved 3;` in its place — wire-safe because no host ever populated it and no service read it, with `runner_pb2` regenerated in the same commit and a canonical guard reading the *descriptor* rather than the `.proto`, so an image built from a stale schema fails rather than disagreeing quietly. **The trial spec's key set moves in both directions, so engine and image must be upgraded together**: `state_checks.expect_initial_state` is a new field a runner image older than this release does not declare, and `state_checks.expected_hash` is one a current image no longer declares — the engine emits its side on **every** pack carrying a non-empty `state_checks:` block, whether or not that pack declares a hash source, so any such pack is rejected at `RegisterTrial` across the skew. Run `make docker-build-core` with the upgrade. Closes #693.

### Removed

- **grading**: **`CheckRunner.result_to_score` is removed; the score a check suite reached is read off the `CheckResultSet` the runner already returns.** It was a second reading of that same object, with two answers no grading substrate shares: `0.5` for a suite whose module failed to load under `fail_on_error: false`, where the runner leaves the component unscored and the core engine scores it `0.0`; and `1.0` for a suite that declared no `@check` at all, where both leave it unscored, because an aggregate over zero verdicts is a vacuous pass rather than a pass. The replacement is the result object itself: `CheckRunner.run(...)` returns a `CheckResultSet` whose `decided_something` says whether the suite reached any verdict, whose `aggregate_score` averages the verdicts it did reach with skips excluded, and whose `passed` / `failed` / `errors` / `skipped` / `total` carry the counts — the same members both grading paths consult; the sentence half is `custom_checks_reason`, which both substrates now emit into `Grade.reasons`. **Nothing in this repository called it** on either grading path, `run_custom_checks` did not (it returns `CheckRunner.run(...)` directly), no component score moves, and no `grading.yaml` key or wire field changes; the removal is visible only to code outside this repository holding a `CheckRunner`.

### Fixed

- **runtime**: **a task-declared stack's runner receives the same credential payload the engine-built stack's runner receives.** `TOLOKAFORGE_SECRETS_JSON` was injected in exactly one place — the engine-built core stack — so a task declaring its own `environment_manifest.stack` got a runner with no credentials at all: its in-container `llm_judge` authenticated with nothing and 401'd on every trial, while host-side agent calls on the same key succeeded. Measured over the shipped `multi_service_lot_ops` compose file under all three network policies: the materialised runner's environment came out as `{'DB_SERVICE_URL': ...}` every time. **Nine tasks across five packs** were affected — every example task that declares a stack (at task or project level) *and* carries an `llm_judge` rubric: `api_endpoint_add`, `db_query_tuning`, `long_debugging_session`, `postgres_upgrade_test`, `schema_isolation_migration` (`example-microservices-pack`), `cache_debug`, `endpoint_add`, `helpdesk_01`, `lot_ops_01`. There is now one credential mechanism with two injection sites: `tolokaforge.secrets` owns the variable name (`CONTAINER_SECRETS_ENV_VAR`) and the payload (`container_secrets_env()`), the engine-built stack reads it as before, and materialisation applies `inject_runner_credentials` to the copied compose file — onto `runner_service` alone, with `$` doubled because Docker Compose interpolates `$` in `environment` values (measured: `"sk-a$bc"` arrives in-container as `'sk-a'`). The escape is one-sided by design: the core stack hands its value to the Docker SDK, which interpolates nothing. The materialised compose file is left mode `0600`, and a host that resolves no secrets leaves it byte-identical so the runner still lazy-inits its own manager. **Two new pack-facing refusals**, both enforced at materialisation and both in service of "only the runner holds the payload". A compose file that declares `TOLOKAFORGE_SECRETS_JSON` on *any* service — as a mapping key, a `KEY=value` entry, or a bare pass-through — now fails naming the service, instead of having its value silently overwritten; the remedy is to delete the entry, since the engine supplies it. And a compose file in which any service bind-mounts the project context root (`.`, `./`) or the compose file itself now fails too: the payload rests in that file, inside the dir every relative bind mount resolves against, and the manifest's load-time bind-mount validator accepts those paths (they are relative, carry no `..`, and stay inside the pack), so a sibling declaring `- .:/ctx:ro` could read the runner's credentials. Mount named paths from a subdirectory instead. Measured over all 33 compose docs under `examples/`, `deploy/` and `tests/`: zero services are refused by either rule. The runner of a task-declared stack is a **pack-authored** image and now receives the operator's full credential payload unconditionally; that widening is written up in `docs/SECURITY.md` § "Task-declared stack (Case B / Case C)". No schema change: `task.yaml`, `grading.yaml`, run-config and the CLI are untouched, and `tolokaforge.secrets` gains two additive exports.
- **grading**: **a `trace_checks` comparison a bound value's type put out of reach fails the candidate event it was read on, not the constraint.** The backstop that reports `the <field> comparison was not made` collected its sentence off every event of the matcher's kind and a single one of them decided the constraint outright, so one junk event anywhere on the trajectory turned a constraint the agent satisfied into a complaint about the author. **Four shapes did it, each measured end to end** through `evaluate_constraint` on `present: {match: {tool: {equals: log}, args: {code: {equals_binding: limit}}}}` over a trajectory that *contains* the satisfying `log(code=4021)`: a **sibling call carrying another type at the same argument path** (`log(code="4021")`); a **junk call to the same tool** (`log(code="x")`); a **call to a tool the matcher's own `tool` predicate rejects** (`audit(code="x")`), which the fold reached before that predicate ever ran; and a **call in a turn the constraint's `within` window excludes**, because the window filtered the matched and undecidable events and passed the comparisons through verbatim. All four scored `passed=False` carrying the authoring message. The reduction now runs **per candidate event, after windowing**: a candidate for a reference is an event the matcher's *other* predicates admit and the window keeps, and the reference is reported only where at least one candidate could not make the comparison and **none of them made it** — which is the printed sentence's own truth, since "the comparison was not made" must not be printed on a trajectory where a candidate made it. A reading over a value no JSON type names — a call that omitted the argument, a JSON `null` — is a third state that neither reports nor silences, so a call that simply left the argument out cannot suppress a report a sibling call earned. **A comparison no candidate could make still forces the constraint to fail**, which is what stops an `absent` or a `negate` from passing vacuously on a reference that selected nothing; a comparison that *was* made and came out false stays the agent's failure and is still scored as one, silently. A matcher declaring two unmakeable references still reports both. **No pack shipped under `examples/` reaches any of the four shapes**: every binding reference in them binds a regex capture or a text-valued argument, so the comparison is made and the backstop never fires, and no recorded bundle in this repository moves. A pack maintained outside this repository whose binder holds a non-string sees exactly those four verdicts move — from a constraint that failed blaming the author to the verdict the agent earned — and `tolokaforge retrace` over such a bundle re-grades to it. The message text, `TraceConstraintResult.undecided`, the wire schema, protocol version, `grading.yaml` surface, CLI and published Python API are unchanged.

### Added

- **core**: `trajectory.yaml` gains `first_user_message_source` — `pinned` when message index 0 is the task's `initial_user_message` delivered verbatim, `simulator` when a user-simulator dispatch wrote it, `null` for a trial that never bootstrapped or a bundle written before the key existed. Backed by `Trajectory.first_user_message_source: FirstUserMessageSource | None`, and logged at INFO on both branches. An analyst can now partition a run's trials into authored-opener and generated-opener without re-reading the task pack. Additive: no reader is required to consume it, and no schema stamp moves. (#1075)
- **core**: a user-simulator reply carrying a leaked reasoning delimiter is discarded and regenerated rather than delivered. `ScratchpadDetector` (`name: "scratchpad"`, reason code `think_tag`) is registered after `FourthWallDetector` in `DEFAULT_REPLY_DETECTORS` and flags a `<think>` / `</think>` tag at a structural position — beginning the reply, or beginning a line — which is where a chat template emits it. A tag mentioned inside a sentence passes, so an LLM-support turn (`My parser chokes on </think> tags in the streamed output`) is delivered untouched. Nothing is stripped: the reply is discarded whole and the simulator asked again, and after three attempts the trial fails as a `harness_error`. Scope, stated as it was measured: the figure behind this is an *opening*-message rate — roughly 1 opening message in 6 on one reasoning simulator, about half of them carrying the delimiter — so a task pinning `initial_user_message` has no such surface at all, the mid-conversation rate is unmeasured, and the untagged half (planning prose with no delimiter) is not covered by any pattern here, nor is any other spelling of the delimiter (`<thinking>`, `<reasoning>`, `<|think|>`). Registration is appended, so every reason code `fourth_wall` already recorded is unchanged. (#1076)
- **core**: `trajectory.yaml` gains `user_reply_guard_events` — one entry per user turn the reply guard did not accept on its first generation, carrying `message_index` (the position in `messages` the turn was dispatched at), `outcome` (`delivered` when a later attempt passed the guard, `refused` when the attempt budget was spent and the trial errored), and one `{detector, reason, excerpt}` per discarded attempt (never an empty list — a turn that discarded nothing records no entry). Both dispatch sites record, and the refusal path records before re-raising, so a trial that died on the guard still carries the evidence. `[]` is the normal state. Additive: no reader is required to consume it, every existing bundle still loads, and no schema stamp moves. (#1077)
- **core**: a per-trial bundle's `task.yaml` names the user actor that drove the trial. Three keys are written after `description`: `interaction_mode` (`conversational` | `agent_only`), `initial_user_message` (the task's pinned opener, verbatim — leading and trailing whitespace included — or `null`), and `user_actor` (the resolved `UserSimulatorConfig`: `mode`, `persona`, `backstory`, and `scripted_flow` in full). `user_actor` is `null` under `agent_only`, which resolves no simulator at all; `interaction_mode` beside it is what separates "no user actor by design" from a defect. What is recorded is the resolution the run used, not what the pack typed — a task declaring no `actors.user` records the defaults that applied, the way `tools`, `policies` and `model_config.<role>.resolved.*` already read. A conversational trial is no longer indistinguishable from an agent-only one, and a scripted or pinned-opener trial no longer loses its user's words to a `prompts.yaml` the simulator never wrote. `user_actor` is inert on a `TaskConfig(**task.yaml)` reload (unknown keys are ignored): it is the record, not an authoring surface. Additive — no reader is required to consume the new keys, every recorded bundle keeps loading, and no schema stamp moves. (#1081)

### Changed

- **core**: `TaskConfig.initial_user_message` is the task's pinned opener — its text is delivered verbatim as the first user message, leading and trailing whitespace included, and no user-simulator dispatch produces that turn. Declaring the key with an empty or whitespace-only value is now refused at load on both authoring surfaces: a `task.yaml` that declares it, and an adapter's `get_task()` that passes it. Previously such a task silently fell back to a generated opening. Migration: give the key text, or leave it unset — omit it in `task.yaml`, pass `None` from an adapter — to have the user simulator open the conversation. Tasks declaring a non-blank opener are unaffected. The bundled `terminal_bench` adapter now passes `None` for a pack carrying no instruction, so those packs keep their generated opening turn. (#1075)
- **core**: a `first_message` key on the user actor is refused at load, naming `initial_user_message` as the field that carries a task's opening turn. Covers every spelling — `actors.user.first_message`, the legacy top-level `user_simulator.first_message`, a project's `task_defaults` actors map, and a direct-Python `UserSimulatorConfig(first_message=…)` — since one predicate runs on both declared mirrors of the user actor. The key was silently dropped before (`ActorSpec` and `UserSimulatorConfig` both ignore extras, and a nested key never reaches the loader's unknown-key warning), so a pack declaring one shipped an opener the agent never saw. Migration: move the text to task-level `initial_user_message`. (#1075)
- **core**: a generated user turn reaches the agent carrying exactly the words the model wrote, or it does not reach the agent at all. `UserReplyGuard` inspects each user-simulator generation for the simulator breaking frame — talking about itself as a machine, or about the exercise as an exercise — and a flagged reply is discarded whole and regenerated rather than edited: no text is excised, truncated or substituted anywhere in the generation path, save one carve-out the guard wraps rather than forbids — `UserSimulator._llm_reply` replaces an empty reply that carried tool calls with a fixed placeholder before the detectors see it, the only text the engine contributes to a user turn, unreachable in-tree (the simulator is handed tool schemas only alongside a `user_tool_executor`, and the conductor always passes `None`) and tracked for removal in #1089. Detection is by attributed frame, not by vocabulary, so `model`, `prompt`, `benchmark`, `ai`, `simulation`, `exercise` and `evaluation` pass through untouched in ordinary support sentences (`My router model is AX3000.`, `This exercise is not showing up in my activity ring.`), and a user describing the *agent* as a machine stays in frame. A demonstrative heading an exercise noun needs its predicate to name an exercise too, and a denial of being a `customer`, `user` or `caller` needs to go on to name the exercise, so `This simulation is crashing every time I open it.` and `I'm not a real customer, I just want a quote before I book.` pass while `This simulation is a roleplay exercise.` and `I'm not a real customer, this is a benchmark.` are caught; the cost is `This simulation is over.` and a bare `I'm not a real customer.`, both missed. The `in`/`for`/`during` frame matches only when the speaker claims a role inside the exercise (`In this benchmark, I am playing a frustrated customer.`), so a break phrased about a third party (`During the simulation the agent refused twice.`) or claiming the role with a copula (`In this simulation, I am the customer, not a developer.`) is missed, as is `represent` outside `roleplay` — it is a modelling verb there (`In the simulation I represent each floor as a single zone.`). Every discarded attempt logs at `WARNING` and rides back on `GenerationResult.guard_rejections`; after three attempts the trial fails as a `harness_error`, in the denominator as our defect. Scripted replies and a task's pinned `initial_user_message` are authored content and never pass through the guard. (#1077)
- **core**: the rate-limit-probe budget invariant reads the reply guard's attempt count. `turn_budget_s` is now `per_call_budget_s + 3 x simulator_per_call_budget_s` and `turn_overshoot_s` is `4 x call_overshoot_s`, so at the documented defaults the per-turn ceiling is `8420 s` (was `5710 s`). Migration: a run config enabling `rate_limit_probe` with an effective episode budget between `5711` and `8420` seconds is now refused at load; the message names the new arithmetic and the two remedies — lower `per_call_budget_s` / `simulator_per_call_budget_s`, or raise `orchestrator.timeouts.episode_s`. No bundled config declares the mode, and the default `episode_s` of `14400` clears the new ceiling. (#1077)
- **core**: the LLM user simulator's `Rules:` block is rewritten, and `trajectory.simulator_schema_version` moves `2` → `3`. The block is now twelve rules led by a precedence clause — the task's `Instruction` outranks every rule below it — and the rules that assumed a mobile-app setting are gone: no rule tells the simulator to state the whole request in its first message, to name the apps or websites its instruction mentions, to track numbered steps, or to correct the agent when it picks the wrong app, item, time or party size. Termination is outcome-based: `###STOP###` is sent once every part of the request has been carried out **or turned down by the agent**, where the old rule held out for the entire goal being satisfied. **This re-baselines difficulty; it is not a no-op.** Two deltas push in opposite directions and confound in any aggregate: dropping the correction rules makes tasks harder (an uncorrected agent mistake now stands, and state hashes mismatch), while outcome-based termination lets a partial-block scenario end with a gradeable transcript instead of running to `max_user_turns`. A pack that relied on the global corrective user without authoring pushback into its own backstory gets harder with no diff in the pack to point at; author the pushback into the backstory to keep it. An analytics consumer comparing runs across the boundary must gate on the stamp — trials stamped `2` and `3` are not comparable, and bundles already written keep the value they recorded. (#1078)
- **runner**: `ENGINE_PROTOCOL_VERSION` 1 → 2. Version 2 is the first that omits `user_simulator.first_message` / `user_simulator.user_context` from the trial spec, so a runner image built from this tree refuses any engine from an earlier release at registration — before any tokens are spent — instead of failing later in Pydantic validation over keys that engine will keep emitting. `make docker-build-core` is part of this upgrade: rebuild the image before rolling the engine, or pin an image tag built from the engine you run. (#1075)

### Removed

- **runner**: `RunnerUserSimulatorConfig.first_message` and `RunnerUserSimulatorConfig.user_context` (the `user_simulator.first_message` / `user_simulator.user_context` keys on the `TaskDescription` wire schema). Neither was read anywhere — the conversation loop is orchestrator-side and reads `TaskConfig.actors.user`. A payload still carrying either key is refused with a message naming both remedies rather than a bare `extra_forbidden`. Migration for an out-of-tree adapter: delete the `first_message=…` argument from its `get_task()` / `to_task_description()` call and set the opener on `TaskConfig.initial_user_message` instead, where it is delivered verbatim as the first user message; `user_context` has no replacement. (#1075)

## v0.18.1 (2026-08-12)

### Feat

- **tbench-adapter**: synthesise EnvironmentManifest from task compose; migrate compose lifecycle to PerTrialRuntimeBackend (#1060)
- **skills**: pre-flight decision extraction + educative PR/umbrella templates (#1034)

### Fix

- **docker**: ship tolokaforge_models sources in the base wheel for wheel-install Docker builds (#1073)

## v0.18.0 (2026-08-12)

### Feat

- **core**: Milestone 29 — tolokaforge-models split (ADR-0030 delivery) (#1058)
- **automation**: let the Slack poller read a header-admission gateway (#1037)
- **llm**: address the gateway in its own dialect and by its own route name (#942)
- **ci**: auto-promote rc images to stable on green rc-smoke (#917) (#918)

### Fix

- **llm**: admit the parameters an operator declares, when litellm's map cannot (#1000)

## v0.16.1 (2026-08-07)

### Feat

- **grading**: composite primary keys in state_checks.id_fields (#924)

### Fix

- **core**: the TypeSense Docker rewrite drops the description cache (#928)

## v0.16.0 (2026-08-06)

### Feat

- **skills**: JSONL progress channel for orchestration subagents (#909)

### Fix

- **grading**: hash-source rule skips a pack whose adapter may supply the source (#911) (#914)
- **llm**: user simulator restarts the conversation after the agent answers (CBT-021) (#905)

## v0.15.0 (2026-08-05)

### Feat

- **grading**: deterministic trace checks, milestone 28 (#890)
- **tools**: optional docker exec --user for compose-variant bash_session + str_replace_editor (#894)
- **tools**: add build_check builtin — zero-arg peer-service HTTP probe (#892)
- **core**: multi-actor architecture — interaction_mode + Actor Protocol + TurnPolicy seam (#868) (#872)

### Fix

- **actors**: AgentOnlyTurnPolicy signals AGENT_DONE on text-only turn (#876) (#877)

## v0.14.2 (2026-08-04)

### Fix

- **docker**: take the runner build context from the builder in core_stack (v0.14.1 still broken) (#864)

## v0.14.1 (2026-08-04)

### Fix

- **docker**: resolve the runner build context on a wheel install (#858)

## v0.14.0 (2026-08-04)

### Feat

- **runtime**: runner wheel split — slim image via subset build target (M15) (#847)
- **secrets**: resolve ${secret:NAME} references in config values (#798)
- **runtime**: Service Readiness Contract — first-class host-invokability boundary (#803) (#817)

### Fix

- **orchestrator**: allow per-trial runs with heterogeneous compose files (#849)
- **runner+orchestrator**: substrate-native support for adapters using compose-variant tools + no DB service (#843)
- **runner-client**: accept degraded runner status + introduce HealthLevel/HealthReport pattern (#801) (#841)
- **grading**: decode wire tool calls in run_custom_checks instead of … (#804)
- **test**: add mkfir and write config before run orchestrator (#802)

## v0.13.1 (2026-08-03)

### Feat

- **slack**: custom message icons, one override parameter per icon role (#724)
- **automation**: report gateway availability and accept a route directive (#723)
- **llm**: route LLM calls through a gateway (LiteLLM proxy), env-configured (#718)
- **grading**: finish runner-side custom_checks as a Pattern-A extension (#704)

### Fix

- **grading**: make the two grading substrates agree — substrate parity, the trajectory record, hash composition, and the combine algebra (#748)
- **automation**: resolve a request against both catalogs, and route every reply icon through the registry (#728)
- **docker**: auto host ports for rag/mock-web; persist rag HF cache on volume (#703)

## v0.13.0 (2026-07-30)

### Feat

- rate-limit probe mode (fixed-interval 429 retry, hours-long budgets) (#665)

## v0.12.0 (2026-07-29)

### Feat

- **adapters**: make rag-service search_kb functional for native tasks (#107) (#666)
- **runtime**: Runner as a distributable service (M14 consolidation) (#642)
- **tools**: configurable working_root on str_replace_editor (#643)
- **adapters**: adapter-declared trial-grader name on orchestrator (#631)

### Fix

- **docker**: widen rag healthcheck start-period to cover model load (#661)
- **docker**: scope mock-web build context to its service files (#654)
- **deploy**: pin linux/amd64 in standalone compose for arm64 hosts (#647)
- **ci**: bind no environment for publish-images dry-run (#646)

## v0.11.2 (2026-07-27)

### Fix

- **runner**: preserve simulator text glued to ###STOP### (closes #611) (#619)

## v0.11.1 (2026-07-27)

### Feat

- **runtime**: runtime independence v1 — expose runner as an independently-usable component (#557)

### Fix

- **runtime**: repair two #557 regressions breaking unit + canonical tests (#615)
- **automation**: resolve-agent prompt - code-shape discipline + code-grounded data-scope (#562)
- **runner**: fail loud on id_fields typos + MCP diff-sync id resolution (#600 follow-ups) (#603)
- **runner**: resolve DB primary-key field from config, not model source (#600)

## v0.11.0 (2026-07-23)

### Feat

- **grading**: judge scoring integrity — verdict consistency, judge customization, offline replay (#528)

## v0.10.0 (2026-07-23)

### Feat

- **cli**: Improved Terminal DX (#460)
- **tools**: persistent agent shell + first-class editor tools (M25 consolidation) (#587)
- **runtime**: per-service network_access opt-out on ServiceSpec (untrusted-sibling partitioning) (#588)

## v0.9.3 (2026-07-22)

## v0.9.2 (2026-07-21)

### Feat

- **project-layer**: Project-layer v1 finalization — canonical shape with warn-only compat (M9) (#531)
- **runtime**: multi-container v1 completion (M8 consolidation) (#511)

### Fix

- **grading**: compare numerically-equal state values as equal (#532)
- **adapter**: fail conversion on invalid output (#494)
- **tools**: advertise PATCH requests (#463)

## v0.9.1 (2026-07-17)

## v0.9.0 (2026-07-17)

### Feat

- **examples,runtime,assets**: multi-container example depth (Milestone 18) (#469)
- **core**: observability seam extension — llm_call trio + model identity (#389) (#450)
- **automation**: model auto-integration pipeline (observe/resolve/finalize + Slack-triggered poller) (#154)
- **project-layer**: make Project schema end-to-end runnable — task-schema relaxation, grading_defaults merge, dead-seam cleanup, docs residue (#375) (#390)
- **skills**: milestone integration-branch workflow with rich consolidation PR (#372)
- **examples**: swap example-microservices-pack backend-api from fictional to postgrest (real image) (#367)
- **runtime**: per-service log capture on trial failure (#302) (#347)

### Fix

- **loader**: preserve storage discriminator tag under run_defaults merge (#312) (#365)

### Refactor

- **core**: extract RunDisplayEvents engine seam to main (#416) (#433)

### Perf

- **orchestration**: reclaim wall-clock in /implement-milestone via overlap, review sharding, and stack warmup (#426)

## v0.8.4 (2026-07-15)

### Feat

- **llm**: configurable hard wall-clock timeout for upstream calls (#327)
- **runtime**: enforce network_policy in docker provisioner + tests (#301) (#336)
- **examples**: runnable reset-recipe pack + end-to-end integration test (#299) (#314)
- **runtime**: Project layer runtime — isolation, reset recipes, capabilities, env identity (#298)
- **dev**: add cbm-onboard / cbm-offboard for codebase-memory-mcp (#266)
- **cli**: tolokaforge assets stamp verb (#263)
- **loader**: ${VAR} interpolation in run configs + --workers CLI flag (#262)
- **schema**: dual-home compute/storage.queue resolution (#241)
- **schema**: actor/seed/capability reservations + task-schema relaxation (#240)
- **schema**: EnvironmentPatch + resolve() + stack sub-object (#232)

## v0.8.3 (2026-07-13)

### Feat

- **loader**: resolve project.yaml + run_configs base+delta merge (#219)
- **schema**: add ProjectConfig, TaskDefaults, RunDefaults + compute/storage/observability blocks (#215)

### Fix

- **deps**: exclude litellm 1.92.0 due to fastapi import regression (#231)

## v0.8.2 (2026-07-10)

### Feat

- **models**: add tencent/hy3 (Hunyuan 3 GA) (#204)
- **models**: add openai/gpt-5.6-terra and openai/gpt-5.6-sol (#203)

## v0.8.1 (2026-07-09)

### Feat

- **models**: add x-ai/grok-4.5 (pricing + capability certificate) (#196)

## v0.8.0 (2026-07-06)

### Feat

- **runtime**: SharedStackRuntimeBackend consumes environment_manifest (#167)
- **runtime**: :local engine-image alias + wire environment_manifest through TaskConfig (#163)
- **core**: TrialExecutor Protocol + wire per-trial substrate bracket (#162)
- **metrics**: roll up judge cost at task and run level (#159)

### Fix

- **docker**: materialize engine wheel via reinstall provider (closes #29, #13) (#176)

### Refactor

- **output**: pin schema_version + int/float wire invariants (closes #152, #153) (#174)
- **output**: typed models for run-level aggregate payloads (stage 1) (#149)
- **orchestrator**: collapse injection kwargs into OrchestratorDeps (#134)
- **docker**: rename ServiceStack → EngineStack; document docker-only + non-Protocol (#169)
- **core**: extract compose-materialisation primitives into shared module (#166)
- **core**: decompose Conductor + extract TrialGrader Protocol (#161)

## v0.17.0 (2026-08-11)

### Feat

- **core**: new public path accessors on [`tolokaforge.core.model_data`](tolokaforge/core/model_data.py) — `bundled_pricing_path()`, `bundled_presets_path()`, `bundled_providers_path()`. Each returns a `pathlib.Path` when the resource exists and raises `FileNotFoundError` when it does not. Stable within v0.17.x; removal or signature change requires a deprecation announcement. Downstream consumers should reach for these instead of raw `importlib.resources` — see [`docs/RELEASING.md § Downstream data-resource consumers`](docs/RELEASING.md#downstream-data-resource-consumers). The pre-cutover `_DATA_ROOT` constant points at `tolokaforge/core/data/`; the models-wheel cutover ([#938](https://github.com/Toloka/tolokaforge/issues/938)) will flip that one line to `tolokaforge_models/data/` with no consumer-side edits. `tolokaforge/core/model_data.py` split into a light seam module + orchestrator-only `model_data_fingerprint.py` sibling so the seam is safe to include in the runner subset. Runner-subset registration for `tolokaforge/core/data/providers.yaml` — a #935 bycatch fix so LLM-judge grading inside a runner-subset image resolves the provider bindings at first use. (#937)
- **llm**: engine-general helpers reached by per-model policy subclasses are now public API — [`coerce_json_strings`](tolokaforge/core/llm/response_policy.py), [`coerce_empty_containers`](tolokaforge/core/llm/response_policy.py), and [`find_additional_properties`](tolokaforge/core/llm/dict_maps.py) (re-exported from [`tolokaforge.core.llm`](tolokaforge/core/llm/__init__.py)). Enables per-model recovery classes to compose the shipped coercion helpers without importing `_`-prefixed engine internals. Stable within v0.17.x; removal or signature change requires a deprecation announcement. Ships alongside `StrictSchema.inline_refs_in_tool` (public overridable classmethod hook for per-tool `$ref` resolution — subclasses that need cycle tolerance override the hook rather than a private method) and six `ClassVar[…]` annotations on the pre-existing `StrictSchema` class-attribute hooks (`KEY_FIELD`, `VALUE_FIELD`, `carry_scalar_dict_map_value`, `flatten_oneof_discriminator`, `strip_parameters_root_description`, `strip_re2_incompatible_patterns`) — a subclass method that mis-writes `self.<hook> = ...` now surfaces as a type-checker error rather than a silent instance-attribute shadow. A canonical import-boundary test at [`tests/unit/llm/test_public_api_boundary.py`](tests/unit/llm/test_public_api_boundary.py) enumerates the eight currently-shipped per-model subclasses / composite classes and rejects any private-symbol reach into `tolokaforge.core.llm.*` — a regression that adds a `_`-prefixed import or a private-method override fires immediately at test-import time. (#936)
- **llm** (Bucket B per ADR-0030 § Docs flip taxonomy): `DictMapHints.build_hints` is now a public instance-method hook on [`tolokaforge/core/llm/prompt_policy.py`](tolokaforge/core/llm/prompt_policy.py) — signature widened from `@staticmethod _build_hints(tools)` to `build_hints(self, tools)`. Enables `RefResolvingDictMapHints` (and future subclasses that want to close over instance state) to override without `# type: ignore[override]`, and clears the base-class shape mismatch flagged in [ADR-0030 § Colleague review focus points, item 9](docs/adr/0030-tolokaforge-models-split.md). Stable within v0.17.x; removal or signature change requires a deprecation announcement. (#936)
- **llm**: provider transport bindings now live in `tolokaforge/core/data/providers.yaml` — Nova's three-site mapping (init `NOVA_API_BASE` `os.environ.setdefault`, `_format_model_name` bare-name return, `_call_with_key_rotation` per-attempt `api_base` / `api_key` / `custom_llm_provider` / slug rewrite), `UNROUTABLE_PROVIDERS` routability, OpenRouter rotation env vars (`OPENROUTER_API_KEYS` / `OPENROUTER_API_KEY`), `custom_llm_provider` litellm hints, and per-provider `rate_limit_patterns` are data-driven. Adding a new provider becomes a `providers.yaml` entry. New public seam: `LLMClient.classify_loop_error(exc)` — bound method that closes over compiled `binding.rate_limit_patterns` so the per-provider patterns thread to `ToolCallingLoop` without crossing the compiled tuple across module boundaries. Schema is [`tolokaforge.core.llm.providers.ProviderBinding`](tolokaforge/core/llm/providers.py) (frozen, `extra="forbid"`); the pre-cutover data file lives at `tolokaforge/core/data/providers.yaml`, and the models-wheel cutover ([#938](https://github.com/Toloka/tolokaforge/issues/938)) will move it to `tolokaforge_models/data/providers.yaml` while widening the `models_fingerprint` payload. (#935)
- **testing**: new public engine seam `tolokaforge.testing.certify` — `Capability`, `ModelCertificate` (widened with `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` / `capability_extras`), `ALL_MODELS`, and the `@register_probe` / `get_probe` dispatch API for out-of-tree probe bodies (#931).
- **observability**: `engine_run_state.json` records the resolved model-data fingerprint — the `models_fingerprint` field carries `{package_version, content_sha256, api_version, minimum_engine_version}` computed from the post-overlay preset table, pricing table, and certificate registry, so a completed run identifies exactly which model-data snapshot it was scored against (#933).
- **llm**: three new policy slots (`assistant_text_policy`, `params_policy`, `message_assembly_policy`) bring `_POLICY_REGISTRIES` to nine. `assistant_text_policy` reshapes `message.content` between litellm parse and `GenerationResult.text` — unblocks the Cohere `<|START_TEXT|>…<|END_TEXT|>` marker case (#929) via an out-of-tree subclass. `params_policy` promotes `ParamsPolicy` to a public base class with a class-body `KNOWN_KEYS` declaration; the overlay validator reads the union across every registered subclass instead of introspecting `GenerationParams.__init__`. `message_assembly_policy` extracts the Nova filler string from `client.py` into a per-instance data field on `NovaMessageAssembly` — the string is now configurable at YAML level via `{name: nova, params: {empty_assistant_filler: "…"}}`. Preset slot values accept both bare `name` (legacy) and `{name, params}` (new — passed to the class constructor); the overlay validator rejects nested-key typos with a `difflib.get_close_matches` suggestion. Additive fields on the `resolve_policy_names` fingerprint: `message_assembly_policy` and `assistant_text_policy` land in `task.yaml.model_config.<role>.resolved.*`; `params_policy` stays intentionally omitted (`GenerationParams` constructor kwargs are already serialised via `model_config.<role>.capabilities`). (#934)

### Fix

- **core** — **Behaviour change**: `reload_pricing(path=<missing>)` and `_load_bundled_presets` now raise `FileNotFoundError` / `ValueError` instead of silently returning `{}` or falling back to defaults. Non-mapping JSON payloads in a pricing file now raise `ValueError` naming the observed type instead of surfacing as an `AttributeError` at the first `.get()` call. Consequence: a downstream caller passing `reload_pricing(path=<maybe-missing>)` will now surface an exception at engine startup — the previous shape silently produced a zero-cost pricing table, which read as `{}` in leaderboards. If the "maybe-missing" behaviour is genuinely wanted, callers must catch the raise themselves. See [ADR-0030 § "Downstream data-resource consumers"](docs/adr/0030-tolokaforge-models-split.md#downstream-data-resource-consumers-new--widening-revised-2026-08-07) for the rationale. (#937)
- **llm**: kill the Nova model-name conditional in `_format_model_name` (Blocker rule 3 antidote follow-up to #934's Gemini removal). Nova's `format_model_name_bare: true` binding field now drives the bare-name return path; no `self.config.provider.lower() == "nova"` string comparison remains in the client. (#935)
- **automation**: `run-probes` renamed the `--path <dir>` flag to `--pyargs <module>` for the moved certification suite (defaulting to `tolokaforge.testing.certify.suite`); `integrate-model.yml` uses the default so no operator-side changes are needed (#931).
- **testing**: `tolokaforge.testing.certify` no longer eagerly imports the pytest fixtures at the package level — runtime callers of the certify seam (e.g. `tolokaforge.core.model_data`) no longer need `pytest` installed. Suite authors continue to reach the fixtures via `pytest_plugins = ["tolokaforge.testing.certify.fixtures"]` or by importing the submodule directly (#931, exposed and fixed via #933).
- **llm**: kill the `if reasoning_name == "gemini"` model-name conditional in `build_capabilities` (AGENTS.md Blocker rule 3 antidote). Gemini's `drop_placeholder_signature` knob now flows through ordinary `{name, params}` dispatch on the `reasoning_codec` slot; the `capabilities: {gemini_drop_placeholder_signature: true}` wire-compat override is rerouted internally in `_apply_config_overrides` — modern preset overlays should declare `reasoning_codec: {name: gemini, params: {drop_placeholder_signature: true}}` instead. (#934)

### Deprecated

- **llm**: bare `name` slot values in preset YAML are deprecated in favour of the `{name, params}` shape. Both are accepted through the v0.17.x cycle and removed in v0.18.0. `_RECOGNISED_OVERRIDE_KEYS` (and the `capabilities:` bespoke override keys it backs — `gemini_drop_placeholder_signature`, `dict_map_prompt_hints`, `supports_typed_dict_maps`, `supports_schema_extras`, `fixed_temperature`, `supports_seed`, `unwrap_input_key`, `reasoning_via_extra_body`) follow the same window and are removed in v0.18.0 (#1017). (#934)

## v0.16.1 (2026-08-07)

### Feat

- **grading**: composite primary keys in state_checks.id_fields (#924)

### Fix

- **core**: the TypeSense Docker rewrite drops the description cache (#928)

## v0.16.0 (2026-08-06)

### Feat

- **skills**: JSONL progress channel for orchestration subagents (#909)

### Fix

- **grading**: hash-source rule skips a pack whose adapter may supply the source (#911) (#914)
- **llm**: user simulator restarts the conversation after the agent answers (CBT-021) (#905)

## v0.15.0 (2026-08-05)

### Feat

- **grading**: deterministic trace checks, milestone 28 (#890)
- **tools**: optional docker exec --user for compose-variant bash_session + str_replace_editor (#894)
- **tools**: add build_check builtin — zero-arg peer-service HTTP probe (#892)
- **core**: multi-actor architecture — interaction_mode + Actor Protocol + TurnPolicy seam (#868) (#872)

### Fix

- **actors**: AgentOnlyTurnPolicy signals AGENT_DONE on text-only turn (#876) (#877)

## v0.14.2 (2026-08-04)

### Fix

- **docker**: take the runner build context from the builder in core_stack (v0.14.1 still broken) (#864)

## v0.14.1 (2026-08-04)

### Fix

- **docker**: resolve the runner build context on a wheel install (#858)

## v0.14.0 (2026-08-04)

### Feat

- **runtime**: runner wheel split — slim image via subset build target (M15) (#847)
- **secrets**: resolve ${secret:NAME} references in config values (#798)
- **runtime**: Service Readiness Contract — first-class host-invokability boundary (#803) (#817)

### Fix

- **orchestrator**: allow per-trial runs with heterogeneous compose files (#849)
- **runner+orchestrator**: substrate-native support for adapters using compose-variant tools + no DB service (#843)
- **runner-client**: accept degraded runner status + introduce HealthLevel/HealthReport pattern (#801) (#841)
- **grading**: decode wire tool calls in run_custom_checks instead of … (#804)
- **test**: add mkfir and write config before run orchestrator (#802)

## v0.13.1 (2026-08-03)

### Feat

- **slack**: custom message icons, one override parameter per icon role (#724)
- **automation**: report gateway availability and accept a route directive (#723)
- **llm**: route LLM calls through a gateway (LiteLLM proxy), env-configured (#718)
- **grading**: finish runner-side custom_checks as a Pattern-A extension (#704)

### Fix

- **grading**: make the two grading substrates agree — substrate parity, the trajectory record, hash composition, and the combine algebra (#748)
- **automation**: resolve a request against both catalogs, and route every reply icon through the registry (#728)
- **docker**: auto host ports for rag/mock-web; persist rag HF cache on volume (#703)

## v0.13.0 (2026-07-30)

### Feat

- rate-limit probe mode (fixed-interval 429 retry, hours-long budgets) (#665)

## v0.12.0 (2026-07-29)

### Feat

- **adapters**: make rag-service search_kb functional for native tasks (#107) (#666)
- **runtime**: Runner as a distributable service (M14 consolidation) (#642)
- **tools**: configurable working_root on str_replace_editor (#643)
- **adapters**: adapter-declared trial-grader name on orchestrator (#631)

### Fix

- **docker**: widen rag healthcheck start-period to cover model load (#661)
- **docker**: scope mock-web build context to its service files (#654)
- **deploy**: pin linux/amd64 in standalone compose for arm64 hosts (#647)
- **ci**: bind no environment for publish-images dry-run (#646)

## v0.11.2 (2026-07-27)

### Feat

- **adapters**: adapter-declared trial-grader name — the orchestrator now loads `adapter.trial_grader_name` (default `"runner_rpc"`, additive, every existing adapter unchanged); adapters shipping a custom `TrialGrader` under the `tolokaforge.trial_graders` entry-point group override it (#620)

### Fix

- **runner**: preserve simulator text glued to ###STOP### (closes #611) (#619)

## v0.11.1 (2026-07-27)

### Feat

- **runtime**: runtime independence v1 — expose runner as an independently-usable component (#557)

### Fix

- **runtime**: repair two #557 regressions breaking unit + canonical tests (#615)
- **automation**: resolve-agent prompt - code-shape discipline + code-grounded data-scope (#562)
- **runner**: fail loud on id_fields typos + MCP diff-sync id resolution (#600 follow-ups) (#603)
- **runner**: resolve DB primary-key field from config, not model source (#600)

## v0.11.0 (2026-07-23)

### Feat

- **grading**: judge scoring integrity — verdict consistency, judge customization, offline replay (#528)

## v0.10.0 (2026-07-23)

### Feat

- **cli**: Improved Terminal DX (#460)
- **tools**: persistent agent shell + first-class editor tools (M25 consolidation) (#587)
- **runtime**: per-service network_access opt-out on ServiceSpec (untrusted-sibling partitioning) (#588)

## v0.9.3 (2026-07-22)

## v0.9.2 (2026-07-21)

### Feat

- **core**: `tolokaforge.core.run_display_events` publishes the `RunDisplayEvents` engine seam — a 9-method `@runtime_checkable` Protocol with `ServiceSnapshot` / `ContainerSnapshot` TypedDicts and a `_NULL_EVENTS` no-op default — that front-ends implement to consume per-trial lifecycle events without pulling any UI package into the engine dependency graph. The orchestrator, conductor, and trial executor emit every lifecycle event through the seam; `OrchestratorDeps.events` accepts a consumer sink (defaults to the null singleton, so runs that never attach a front-end are byte-identical to the pre-seam engine) (#416)
- **runtime**: `RuntimeBackend` widens with `get_infrastructure_snapshot(handle) -> list[ContainerSnapshot]` — the display's per-trial infrastructure hook. `PerTrialRuntimeBackend` reads its per-trial compose stack; `SharedStackRuntimeBackend` returns `[]` in built-in mode and reads the run-wide compose otherwise; `InMemoryRuntimeBackend` returns a synthetic single-container shape. Every in-repo backend implements the new method in the same commit, so out-of-tree implementers of `RuntimeBackend` (none on `main`) would need to add the method to keep `isinstance(impl, RuntimeBackend)` semantically complete (#416)
- **dx**: `--display=rich` panel gains a compact **Boot log** region during the Docker startup window. When `_total_trials == 0` and the ring buffer contains any `tolokaforge.docker.*` record, `LiveRunDisplay` renders a `Panel(title="Boot log")` between the services widget and `main` listing the last five docker milestones, most-recent-last, as `HH:MM:SS.mmm | short-name | message` (UTC-stable). The region steals rows from `main` under a stable-height clamp — `budget = total - services_h - bottom_h - 5`; below three rows it drops entirely — so the total renderable height stays `max(12, viewport - 1)` and Rich Live never re-anchors (regression guard for the #392 stacking fix). The region disappears the moment trials dispatch. See [docs/CLI.md](docs/CLI.md) § Live run panel. (#394)
- **dx**: Full TUI mode (`tolokaforge run --display=full`) — a Textual `App` (`tolokaforge.dx.tui.TextualRunApp`) that consumes the same `RunDisplayEvents` seam the Rich Live panel does and renders a keyboard-navigable, tabbed run view. Header + one-line status bar, left-pane scrollable trial list (`j`/`k` or `↑`/`↓`; `PgUp`/`PgDn` for ~20-row jumps; `Home`/`End` for first/last), right-pane focused-trial summary with per-trial infrastructure. Bottom `TabbedContent` — **Overview** (banner, phase, services summary), **Logs** (`RichLog` fed by the shared ring buffer), **Services** (`DataTable` of engine-stack services), **Infra** (per-focused-trial containers), **Errors** (WARNING+ filtered). Tab keys `1`–`5`; `l` jumps to Logs; `?` toggles a modal help screen; `q` exits the UI (Ctrl-C still kills the run). Requires `pip install 'tolokaforge[dx]'` — `textual>=0.85.0` is now in the `[dx]` extras. `LiveRunDisplay.for_mode(DisplayMode.FULL)` returns the Textual app when textual is importable and falls back to the Rich `LiveRunDisplay` with a WARNING log line otherwise. See [docs/CLI.md](docs/CLI.md) § Full TUI and [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md).
- **dx**: `tolokaforge` invoked with no subcommand (and the explicit `tolokaforge repl` verb) drops into an interactive Click REPL. Free-form passthrough of every top-level command: tab-completion of subcommands, flag names, and file paths; command history at `~/.tolokaforge_history`; exit via `exit`, `quit`, or Ctrl-D. Root flags supplied at REPL entry (`-v`, `-q`, `--display`, `--log-format`) apply to every command until the session exits — they mutate global logging + console state once via the `cli()` callback and stay in effect. Dependencies added to the `[dx]` extras: `click-repl>=0.3.0`, `prompt-toolkit>=3.0.51`. Library-only installs (`pip install tolokaforge`) are unaffected; running `tolokaforge` without the `[dx]` extras prints the install hint served by `tolokaforge._entry:main`. See [docs/CLI.md](docs/CLI.md) § Interactive shell.
- **cli**: `tolokaforge run --resume --run-dir <path>` now works — the CLI resolves the existing run dir (fixing the pre-milestone bug where a fresh timestamped dir was always allocated), loads `run_state.json`, and re-runs only pending/infrastructure-failed trials. Idempotent on a fully-complete run (`Nothing to do; run already complete`). The A5 start banner shows `→ Resume: <run-id>` on the resume path. Worker restart on a populated queue (`tolokaforge worker --run-dir <existing>`) resumes automatically via the durable queue. See [docs/CLI.md](docs/CLI.md) § Resume. (#286)
- **cli**: `tolokaforge run --dry-run [--dry-run-samples N]` (default N=3) resolves config + tasks with full parity to a real run, renders the first N samples as Rich panels (system prompt, user prompt, tool spec, resolved model / judge / runtime), and exits 0 without any provider HTTP call. Silenced under `--display=none`. See [docs/CLI.md](docs/CLI.md) § Dry run. (#284)
- **cli**: `tolokaforge run` gains `--cost-limit`, `--time-limit`, `--sample-limit`, `--fallback-models`, `--model-cost-config`. Any budget hit triggers graceful shutdown: in-flight trials finish, `LIMIT_HIT.json` is written under the run dir, and the A5 end banner shows `⏸ Run stopped (<reason>)`. The B1 cost-meter turns amber at 80% and red at 100% of `--cost-limit`. `--fallback-models` implements an ordered per-generate cursor letting a batch survive provider outages. `--model-cost-config` overlays JSON/YAML onto the shipped pricing table. See [docs/CLI.md](docs/CLI.md) § Cost, time, and sample limits and § Fallback models. (#283)
- **cli**: `tolokaforge run` prints a two-line start banner (`→ Run: <run-id>` + `→ Report: file:///…/`) on stderr before the run, and a three-line end banner (`✓ Run complete in <duration>` or `✗ Run failed in <duration>` + `→ Report: file:///…/` + `→ Browse: tolokaforge browse <run-id>`) after — including on failure. URLs are OSC 8 hyperlinks. Silenced under `--display=none`. `Orchestrator.run` now accepts pre-resolved `run_id` / `output_dir` kwargs. See [docs/CLI.md](docs/CLI.md) § Run banner. (#281)
- **cli**: `tolokaforge --version` prints the installed package version. Root `tolokaforge --help` groups commands under **Runs / Tasks / Docker / Config / Assets / Adapters** headings, alphabetical within each. See [docs/CLI.md](docs/CLI.md) § Root help layout. (#278)
- **cli**: `--display=rich` now renders a Rich Live progress panel during `tolokaforge run` — left pane shows per-trial status (`⏳` running, `✓` completed, `✗` failed), right pane shows the focused (most-recently-transitioned) trial's cumulative summary (`turn N · in Xk / out Y tok · $Z.ZZ · last: <event_kind>`), bottom bar shows `{completed}/{total} · {running} running · ${cost} · in {prompt} / out {completion} tok · fail {failed} · eta {eta}`. Under `--display={plain,log,none}` (or non-TTY under `rich`) the display is a no-op context manager and the log-line stream is preserved. Consumers subscribe via the `tolokaforge.core.run_display_events.RunDisplayEvents` Protocol (`run_started`, `trial_started`, `trial_progress`, `trial_completed`, `trial_failed`, `judgment_scored`, `run_finished`) threaded through `OrchestratorDeps.events`; the orchestrator, conductor, and runner emit into it and `tolokaforge.dx.live_panel.LiveRunDisplay` is the reference terminal front-end consumer. See [docs/CLI.md](docs/CLI.md) § Display modes and [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md). (#285)
- **cli**: root flag `--display={full,rich,plain,log,none}` and env var `TOLOKAFORGE_DISPLAY=…` pick the overall stderr UI. Auto-selects `plain` when `CI` is set or when `sys.stderr` is not a TTY; auto-selects `rich` on a TTY. `--display=none` silences stderr on success while preserving the stdout artifact-path emission. `--display=full` falls back to `rich` when textual is not installed. Orthogonal to `--log-format`. See [docs/CLI.md](docs/CLI.md) § Display modes. (#282)
- **cli**: `tolokaforge run` and `tolokaforge prepare` emit the absolute run-dir path as a single line on `sys.stdout` on success. Read-only commands (`status`, `validate`, `config validate`, `assets stamp`, `worker`, `adapter convert`, `analyze`, `docker *`) leave `sys.stdout` empty. Idiom: `RUN_DIR=$(tolokaforge run --config …)`. See [docs/CLI.md](docs/CLI.md) § stdout / stderr contract. (#280)
- **logging**: structured console format `HH:MM:SS.mmm | LEVEL | k=v | message` with root `--verbose` / `--quiet` / `--log-format={pretty,plain,json}` flags; auto-select `pretty` on TTY / `plain` on pipe; ANSI palette matches `_display.THEME`. See [docs/CLI.md](docs/CLI.md) § Structured logging. (#279)
- **schema**: task.yaml minimal shape is task_id + description; initial_state / tools / user_simulator / grading now optional with sane defaults (#366)
- **runtime**: `compute.log_tail` + `compute.capture_logs_on_success` config knobs and a per-service compose-log capture primitive for trial-failure diagnostics (#302)
- **runtime**: `PerTrialRuntimeBackend` captures per-service logs on provision-stage failure (compose-up / reset-recipe) before teardown, writing `services/<service>.log` + a `services/_capture.yaml` manifest; `RuntimeBackend` gains `capture_service_logs` (per-trial writes `.log` files; shared-stack is a documented no-op) (#302)
- **runtime**: on a trial-body failure (`ERROR` / `TIMEOUT`) the trial executor captures per-service logs before teardown, emits a `trial.service_logs_captured` summary line, and amends the trial's `metrics.yaml` with a `captured_service_logs` byte-count map (#302)
- **examples**: `multi_service_slow_start` pack + `test_startup_order_stress.py` stress-cover the `depends_on` + healthcheck + `--wait` start-order chain against a `pg_sleep`-driven ≥20 s slow dependency, proving the per-trial backend blocks on the full chain before the trial's first RPC (#303)

### Fix

- **dx**: `--display=rich` no longer stacks duplicate copies of the `LiveRunDisplay` panel during trial execution. `LiveRunDisplay.__enter__` now sweeps every non-root logger in `logging.root.manager.loggerDict` (skipping `PlaceHolder` entries) and removes any `StreamHandler` bound to the captured pre-Live terminal streams; loggers with `propagate=False` additionally receive a `_LogSink` so their records still surface. This closes the channel through which litellm's private `LiteLLM` / `LiteLLM Router` / `LiteLLM Proxy` `StreamHandler`s bypassed Rich Live's cursor coordination. `__exit__` restores every removed handler. See [docs/CLI.md](docs/CLI.md) § Live run panel. (#392)
- **grading**: a project's `task_defaults.grading_defaults.combine` now deep-merges under each task's own `grading.yaml.combine` (task fields win, `weights` merge key-by-key); a task that omits `combine` inherits the project block instead of an arbitrary `{state_checks: 1.0}` / `pass_threshold: 1.0` fallback, and `get_grading_config` no longer raises on tasks that ship no `combine` block (#376)
- **runtime**: `SharedStackRuntimeBackend` no longer advertises `reset_recipes:*` capabilities — a shared stack cannot honour them (reset tasks route to `PerTrialRuntimeBackend`, which still advertises them). A shared-selected run that requested a `reset_recipes:*` capability was admitting a capability it could not deliver; it is now refused at run start with the standard admission error (#310)
- **runtime**: `network_policy: limited_internet` enforcement via a squid forward-proxy sidecar. Declare `stack.limited_internet_allowlist: [host, ...]` (bare hostnames or `*.domain` wildcards); the provisioner injects a digest-pinned `ubuntu/squid` sidecar on a dual internal/edge network, points app services' `HTTP(S)_PROXY` at it, and default-denies non-allowlisted egress with HTTP 403. Runner retains direct edge egress for `llm_judge` grading (#323).
- **manifest**: five endpoint-resolution override fields on `stack:` — `runner_port`, `db_service`, `db_port`, `rag_service`, `rag_port` — let task-pack authors point the engine at non-convention service names and ports without touching the runtime backend. Defaults reproduce prior behaviour byte-identically; unknown service overrides fail loud at manifest load (#144).
- **observability**: `aggregate.json` gains an additive `captured_service_logs` roll-up on `RunAggregate` — a run-level view (`captures`, `total_bytes`, `per_service_bytes`, per-bundle `entries`) of the per-trial and run-level captured compose-log surfaces, with a closed `source` vocabulary (`provision_failure` / `trial_body` / `shared_stack_materialise`). Produced at report generation by scanning the on-disk capture tree (per-trial `services/_capture.yaml` and `metrics.yaml`, plus the run-level shared-stack `services/_capture.yaml`); fail-safe — a corrupt capture artifact is skipped, never breaking report generation. Always emitted (zero envelope on clean runs); no `schema_version` bump (#337).
- **runtime**: `SharedStackRuntimeBackend._materialise_manifest` now captures per-service compose logs to `<output_dir>/services/<name>.log` + `_capture.yaml` (with `capture_reason: "materialise_error"`) before cleanup on the failure path — mirroring #302's per-trial pattern for run-level materialise failures (#339).
- **observability**: per-trial `provisioning_duration_s` recorded in `metrics.yaml` — wall-clock seconds around the `provision → await_ready → endpoints` bracket, monotonic-clock-measured, additive to the existing metrics shape (#354).
- **runtime**: provision-failed trials now write a minimal trial bundle (`trajectory.yaml` + `metrics.yaml` with `error: "provision_error"` + `grade.yaml`) to `<output_dir>/trials/<task>/<idx>/`, making cost aggregation and post-mortem tooling see a consistent trial-directory shape whether the trial completed or failed to provision (#338).
- **project-layer**: M9 keystone — canonical Project-layer shape activated with **warn-only** compat. Every legacy shape a real task pack ships continues to load unchanged, with a `DeprecationWarning` naming the file, the offending key, and the concrete migration action. **No hard breaks in this release.** Post-M9 follow-up #533 tracks the future strict-rejection flip once a deprecation-window release cycle has closed.

  Canonical shapes activated (aliases still accepted with warning):
  - **`actors.user`** is the canonical author shape for the user simulator on `project.yaml` `task_defaults` and `task.yaml`; it now drives the simulator at runtime (previously parsed but inert). The top-level `user_simulator` block is a legacy alias — the loader lifts it into `actors.user` per config layer with a `DeprecationWarning`. Direct-Python callers using `TaskConfig(user_simulator=...)` continue to work via a `mode="before"` shim on `TaskConfig` and `TaskDefaults` that lifts to `actors["user"]` with the same warning (#213).
  - **`evaluation.projects`** replaces `evaluation.task_packs`. Legacy key accepted with warning.
  - **`network_policy` lowercase enum values** (`no_internet`, `limited_internet`, `full_internet`) replace the uppercase names (`NO_INTERNET`, ...). Uppercase accepted with warning; lowercased at parse time.
  - **`security_context_defaults.run_as_user` / `.run_as_group`** replace `.user` / `.group`. Legacy keys accepted with warning; disagreeing values (both a legacy and a canonical key set to different values) fail loud.
  - **`stack` sub-object** on `default_environment` is the canonical substrate shape; flat `compose_file` / `runner_service` at the top level of `EnvironmentPatch` are accepted with warning.

  Compat surfaces preserved:
  - **Missing `project.yaml`** — a pack without one still loads via a synthesised default. The synthesiser emits a `DeprecationWarning` naming the searched root and the exact fix (`Add a project.yaml at the pack root...`). Post-M9 #533 will flip this to a hard error.
  - **Unknown keys** in `project.yaml` / `run_configs/*.yaml` / `task.yaml` / `grading.yaml` emit a `DeprecationWarning` naming the file, the key, and the closest schema match (e.g. `unknown key 'mox_turns' in dev.yaml — did you mean 'max_turns'? Rename 'mox_turns' to 'max_turns'... (tracked in #533)`) — the key is silently dropped from the model instance so existing configs keep loading. Top-level scan only; nested unknown keys pass through unnoticed (documented limitation; the recursive scan will land alongside the strict flip in #533).
  - **`stack: null` / `stack.compose_file: null`** in a task's `environment_manifest` (and in a project's `default_environment`) now emit a `DeprecationWarning` naming the offending file and field, with the documented full-override rule — a task cannot unset the environment (or its substrate pointer) out from under a project that declares one. Omit the key entirely to inherit. Post-M9 #533 will re-flip to a hard error.

  In-tree canonicalisation:
  - Every pack under `examples/native/` now ships a `project.yaml` at pack root, uses the `stack` sub-object, `actors.user`, `run_configs/<name>.yaml`, and `evaluation.projects`. `example-microservices-pack` is the reference exemplar.

  Every deprecation message here follows a uniform actionable shape: **what** legacy shape triggered the warning, **where** it lives (file basename via `source_context` — never absolute paths), **why** it is deprecated, **how** to migrate (a concrete key rename or block move with a worked example), and **when** it goes away (`(tracked in #533)`). This lets external pack authors migrate incrementally without any hard breaks and gives them a concrete follow-up issue to subscribe to (#213).

- **docs/security**: rewrite `SECURITY.md`'s architecture overview, threat table, testing, and checklist to reflect the actual `runner-net` (non-internal, docker-py `EngineStack`) model. The doc previously described a vanished `env-net` (`internal: true`) network and `docker-compose.yaml`, and listed "executor reaching external internet — addressed by `env-net internal:true`" as an addressed threat that no longer holds (#324).
- **loader** (M9): project `task_defaults` again loads the shipped `example-microservices-pack`. Only `TaskConfig`-shaped keys of `task_defaults` are merged into each task dict before validation; project-scoped-only keys (`grading_defaults`, `continue_prompt`) are excluded from that merge and reach the engine through their own seams. The excluded set is derived from the schema (`TaskDefaults.model_fields - TaskConfig.model_fields`), so future project-only defaults are handled automatically (#277).
- **loader** (M9): `stack: null` and `stack: {compose_file: null}` in a task's `environment_manifest` (and in a project's `default_environment`) now emit a `DeprecationWarning` naming the offending file and field, with the documented full-override rule — a task cannot unset the environment (or its substrate pointer) out from under a project that declares one. Omit the key to inherit. Strict rejection deferred to a future release (#235, #533).

### Docs

- **guide**: task-pack image-layering guide covering the 3-tier base/environment/instance pattern from SWE-bench, with Dockerfile snippets and compose-file references (#146).
- **security/runtime**: document the built-in (Case A) `EngineStack` as `full_internet` by construction — the built-in `runner-net` is non-internal and the runner retains egress for in-container LLM-as-judge grading; task-declared stacks remain the only path with an enforceable `network_policy`. Recorded in ADR-0018 + `RUNTIME_BACKENDS.md`, and locks the `Network.internal` foundation primitive + the `EngineStack.create_networks` non-internal invariant with a unit test (#324).

### Compat / migration notes

Every soft-warning path M9 introduces is documented as a `DeprecationWarning` that names the file, the offending key/shape, the concrete migration action, and a `(tracked in #NNN)` suffix pointing at the follow-up issue that carries the retirement schedule:

- **#533** — post-M9 strict flip: re-flip Project-layer `extra="forbid"`, remove `synthesize_default_project`, re-flip `stack: null` / `stack.compose_file: null` to hard errors, add the recursive unknown-key scan. Fires one release cycle after M9 lands.
- **#534** — post-M9 `orchestrator.max_turns` default flip (redo #265): flip default from `int = 50` back to `int | None = None` (opt-in cap) after the deprecation window closes.
- **#214** — M5 legacy alias retirement (pre-existing): removes `evaluation.task_packs`, top-level `user_simulator`, uppercase `network_policy`, `security_context.user/group`, flat-stack aliases.
- **#489** — `orchestrator.timeouts` opt-in default (sibling of #265): bundled with M5's `turn_s`/`episode_s` → `trial_seconds`/`tool_call_seconds` rename so the field reshapes once.

External pack authors: run your suite; every warning message tells you exactly what to change. There are no schema errors introduced in this release — a pack that loads on `main` today continues to load, with warnings that point at the migration you'll need to make before the strict flips in #533 / #534 / #214 / #489 land.

Release-summary anchors (short form of items detailed above):

- **project-layer**: Project-layer v1 finalization — canonical shape with warn-only compat (M9) (#531)
- **runtime**: multi-container v1 completion (M8 consolidation) (#511)

Additional fixes landed this release:

- **grading**: compare numerically-equal state values as equal (#532)
- **adapter**: fail conversion on invalid output (#494)
- **tools**: advertise PATCH requests (#463)

## v0.9.1 (2026-07-17)

## v0.9.0 (2026-07-17)

### Feat

- **examples,runtime,assets**: multi-container example depth (Milestone 18) (#469)
- **core**: observability seam extension — llm_call trio + model identity (#389) (#450)
- **automation**: model auto-integration pipeline (observe/resolve/finalize + Slack-triggered poller) (#154)
- **project-layer**: make Project schema end-to-end runnable — task-schema relaxation, grading_defaults merge, dead-seam cleanup, docs residue (#375) (#390)
- **skills**: milestone integration-branch workflow with rich consolidation PR (#372)
- **examples**: swap example-microservices-pack backend-api from fictional to postgrest (real image) (#367)
- **runtime**: per-service log capture on trial failure (#302) (#347)

### Fix

- **loader**: preserve storage discriminator tag under run_defaults merge (#312) (#365)

### Refactor

- **core**: extract RunDisplayEvents engine seam to main (#416) (#433)

### Perf

- **orchestration**: reclaim wall-clock in /implement-milestone via overlap, review sharding, and stack warmup (#426)

### Notes for embedders

- **`Orchestrator.run()` now returns the resolved `Path` of the run dir it created** (previously `None`). Callers that ignore the return value are unaffected — Python drops it silently. Callers that assign the result now hold a `Path` instead of `None`; update `results = orchestrator.run()` to `run_dir = orchestrator.run()` (or discard it). See [docs/API.md](docs/API.md) § Orchestrator. (#280)

### Breaking Changes

1. **`tolokaforge.cli.*` modules renamed to `tolokaforge.dx.*`.** The Click command tree, Rich panels, banners, and dry-run renderer are now the reference terminal front-end and live under a namespace whose name signals their pluggability role — the same `RunDisplayEvents` Protocol (in `tolokaforge.core.run_display_events`) admits alternate front-ends. Module map: `tolokaforge.cli._display` → `tolokaforge.dx._display`; `tolokaforge.cli._run_display` → `tolokaforge.dx.live_panel`; `tolokaforge.cli._run_banner` → `tolokaforge.dx.banners`; `tolokaforge.cli._dry_run_render` → `tolokaforge.dx.dry_run_render`; `tolokaforge.cli.main` → `tolokaforge.dx.cli.main`; `tolokaforge.cli.docker_commands` / `adapter_commands` / `config_commands` / `assets_commands` → `tolokaforge.dx.cli.{docker,adapter,config,assets}`. Rich is now an optional dep behind `pip install 'tolokaforge[dx]'`; the `tolokaforge` console script is served by a stdlib-only shim (`tolokaforge._entry:main`) that prints an install hint if the extras are missing. Library-only imports (`from tolokaforge.core.orchestrator import Orchestrator`) are unaffected. See [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md).
2. **`StructuredLogger` console output moves from `sys.stdout` to `sys.stderr`.** Every tolokaforge log record — including the `orchestrator`, `runner`, `output_writer`, and adapter records that previously wrote to `stdout` via `StructuredLogger`'s private handler — now propagates through the root handler installed by `configure_root_logging`, which writes to `sys.stderr`. Downstream consumers piping tolokaforge's stdout to capture log lines should switch to `2>&1 | …` or to `--log-format=json` (still on stderr). Aligned with the `stdout=artifact` carveout in #280.
3. **Console log line shape changed to `HH:MM:SS.mmm | LEVEL | k=v | message`.** The legacy `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"` format (seconds resolution, inline `(k=v, k=v)` in the message string) is gone. Machine consumers grepping the old shape need to update to the new column layout — the pipe-separated columns, ANSI palette, and JSON schema are pinned by canonical goldens under `tests/canonical/golden/logging/`. (#279)
4. **`tolokaforge run` with zero tasks now exits with code `1`** (previously exited `0` with a red "No tasks found!" line on stderr). Callers relying on the silent-success behaviour should either pre-filter empty task sets or handle the non-zero exit. (#280)

## v0.8.4 (2026-07-15)

### Feat

- **llm**: configurable hard wall-clock timeout for upstream calls (#327)
- **runtime**: enforce network_policy in docker provisioner + tests (#301) (#336)
- **examples**: runnable reset-recipe pack + end-to-end integration test (#299) (#314)
- **runtime**: Project layer runtime — isolation, reset recipes, capabilities, env identity (#298)
- **dev**: add cbm-onboard / cbm-offboard for codebase-memory-mcp (#266)
- **cli**: tolokaforge assets stamp verb (#263)
- **loader**: ${VAR} interpolation in run configs + --workers CLI flag (#262)
- **schema**: dual-home compute/storage.queue resolution (#241)
- **schema**: actor/seed/capability reservations + task-schema relaxation (#240)
- **schema**: EnvironmentPatch + resolve() + stack sub-object (#232)

## v0.8.3 (2026-07-13)

### Feat

- **loader**: resolve project.yaml + run_configs base+delta merge (#219)
- **schema**: add ProjectConfig, TaskDefaults, RunDefaults + compute/storage/observability blocks (#215)

### Fix

- **deps**: exclude litellm 1.92.0 due to fastapi import regression (#231)

## v0.8.2 (2026-07-10)

### Feat

- **models**: add tencent/hy3 (Hunyuan 3 GA) (#204)
- **models**: add openai/gpt-5.6-terra and openai/gpt-5.6-sol (#203)

## v0.8.1 (2026-07-09)

### Feat

- **models**: add x-ai/grok-4.5 (pricing + capability certificate) (#196)

## v0.8.0 (2026-07-06)

### Feat

- **runtime**: SharedStackRuntimeBackend consumes environment_manifest (#167)
- **runtime**: :local engine-image alias + wire environment_manifest through TaskConfig (#163)
- **core**: TrialExecutor Protocol + wire per-trial substrate bracket (#162)
- **metrics**: roll up judge cost at task and run level (#159)

### Fix

- **docker**: materialize engine wheel via reinstall provider (closes #29, #13) (#176)

### Refactor

- **output**: pin schema_version + int/float wire invariants (closes #152, #153) (#174)
- **output**: typed models for run-level aggregate payloads (stage 1) (#149)
- **orchestrator**: collapse injection kwargs into OrchestratorDeps (#134)
- **docker**: rename ServiceStack → EngineStack; document docker-only + non-Protocol (#169)
- **core**: extract compose-materialisation primitives into shared module (#166)
- **core**: decompose Conductor + extract TrialGrader Protocol (#161)

## v0.7.0 (2026-07-02)

### Feat

- **core**: PerTrialRuntimeBackend + trial-isolation enforcement + --runtime CLI (#148)

### Fix

- **db-service**: support JSONPath filter expressions in /query (#157)

## v0.6.0 (2026-07-02)

### Feat

- **grading**: diff-first default state view for the rubric judge (#151)

## v0.5.0 (2026-07-02)

### Feat

- **core**: RuntimeBackend provisioning contract (ADR-0010) (#133)
- **core**: add EnvironmentManifest typed schema for multicontainer environments (#121)

### Fix

- **orchestrator**: select full_stack when the adapter declares rag-service need (#140)

### Refactor

- **runtime**: move per-trial RPC methods onto RuntimeBackend (ADR-0013) (#141)
- **runtime**: promote RunnerClient to a Protocol; rename concrete to GrpcRunnerClient (#135)
- **core**: EnvironmentManifest as compose-as-source-of-truth (#139)

## v0.4.1 (2026-07-01)

### Feat

- **llm**: register anthropic/claude-sonnet-5 (cert + pricing) (#129)

### Fix

- **pricing**: refresh GLM 5.1/5.2 rates to current OpenRouter list (#123)

## v0.4.0 (2026-06-30)

### Feat

- **orchestrator**: make TrialArtifactWriter injectable (#112)

### Fix

- **core**: decouple TrialSpec.run_id from output_dir.name (#111)

### Refactor

- **core**: lift _run_trial behind a typed Conductor Protocol (#101)

## v0.3.1 (2026-06-26)

### Fix

- **grading**: faithful judge KB search — judge reads the same KB the agent did (#95) (#102)

### Refactor

- **core**: lift DockerRuntime behind a typed RuntimeBackend Protocol (#96)
- **grading**: relocate LLM-judge model from rubric to run config (#98)
- **trial**: type env_endpoints with EnvEndpoints Pydantic model (#92)

## v0.3.0 (2026-06-26)

### Feat

- **grading**: structured rubric grading via a runner-side read-only agentic judge (#94)
- **output**: formalize RunAggregateWriter as the run-level data-plane seam (#85)
- **output**: formalize TrialArtifactWriter as the typed data-plane seam (#79)
- **core**: define TrialSpec / TrialResult as the typed control↔trial seam (#74)
- **devcontainer**: add Dev Container config for reproducible dev env (#81)

### Fix

- **docker**: unblock clean runner/rag-service builds and integration tests (#88)
- **ci**: pin Claude review action to claude-opus-4-8 (#78)

### Refactor

- **runner**: drop private-package prefix from MCP_ASYNC import path (#73)

## v0.2.11 (2026-06-18)

### Feat

- **adapters**: register migration_bench constant in AdapterType (#71)
- **llm**: register z-ai/glm-5.2 and moonshotai/kimi-k2.7-code (cert + pricing) (#72)

## v0.2.10 (2026-06-17)

### Feat

- **presets**: operator-overridable preset overlay file (#69)
- **llm**: add OpenRouter provider routing to ModelConfig (#68)

### Fix

- **grading**: make unknown jsonpath operators fail loud + deterministic reasons (#66)

### Refactor

- **adapters**: make the runner adapter-agnostic (plugin-first) (#61)

## v0.2.9 (2026-06-16)

### Feat

- **llm**: register nemotron-3-ultra-550b-a55b (cert + pricing) (#65)

## v0.2.8 (2026-06-16)

### Feat

- **llm**: recover MiniMax-M3 tag-conversion corruption (#55)

## v0.2.7 (2026-06-10)

### Feat

- **llm**: register anthropic/claude-fable-5 (#52)

## v0.2.6 (2026-06-08)

### Feat

- **llm**: register minimax/minimax-m3 with codec-only preset (#51)

## v0.2.5 (2026-06-08)

### Fix

- **adapters**: restore bundle_writer so `adapter convert` works (#48)

## v0.2.4 (2026-06-05)

### Feat

- **llm**: register 7 arena-lineup models with preset routing (#46)
- **release**: automate releases with commitizen (cz bump) (#41)

## v0.2.3 (2026-06-04)

### Added

1. **`deepseek/deepseek-v3.2-exp` support.** New `ModelCertificate` (14 required / 6 known_unsupported, live-certified 2026-06-03) plus a dedicated `deepseek_v32` preset routing the experimental V3.2 line through the OpenAI reasoning codec. Unlike the V4 line it round-trips dict-map and discriminated-union tool calls on the standard response policy, so it needs neither `json_coerce` nor `dict_map_hints`; pricing was already present in `pricing.json`. (#36)

### Fixed

1. **`tolokaforge.__version__` reconciled** to match `pyproject.toml` (it had lagged at `0.2.1` through the 0.2.2 release).

## v0.2.2 (2026-06-03)

### Fixed

1. **Wheel resolver — relocated uv cache.** The Docker runner is provisioned from a host-resolved `tolokaforge` wheel; for a git-source install the `pip-cache` provider recovers the wheel `uv` built during `uv sync`. `_walk_pip_wheel_caches()` hard-coded `~/.cache/uv`, so when the cache was relocated (e.g. `astral-sh/setup-uv` sets `UV_CACHE_DIR` in CI) the wheel was missed and `tolokaforge run` failed at service start-up with `NoWheelError`. Cache *location* is now discovered via `uv cache dir` / `UV_CACHE_DIR` / `PIP_CACHE_DIR` (with the `~/.cache` defaults preserved); the uv-internal layout scan is unchanged, and `NoWheelError` now reports the caches it searched. (#27, #28)
2. **Adapters package export.** Removed the stale `FrozenMcpCoreAdapter` entry from `tolokaforge.adapters.__all__` (it is no longer importable from the engine), which had broken `from tolokaforge.adapters import *`. (#14)

## v0.2.1 (2026-05-29) — LLM Reasoning & Observability Overhaul

### Breaking Changes

1. **`ModelConfig.reasoning`** migrated from bare string to `ReasoningConfig` struct. YAML configs using `reasoning: "medium"` now raise `ValidationError` at load time — migrate to `reasoning: {mode: adaptive, effort_hint: medium}` or equivalent. See [`docs/CONFIG.md`](docs/CONFIG.md) § `reasoning:`.
2. **`GenerationResult.token_usage: dict`** removed — replaced by `GenerationResult.usage: Usage` (full Anthropic + OpenAI accounting incl. cache + reasoning tokens).
3. **`Metrics.tokens_input`** / **`Metrics.tokens_output`** removed — replaced by `Metrics.usage: Usage`. Aggregators expose `avg_<field>` / `total_<field>` per `Usage` field (e.g. `avg_reasoning_tokens`, `total_cache_read_input_tokens`).
4. **`Message.reasoning: str`** migrated to `Message.reasoning: StructuredReasoning | None` — preserves provider signatures + block types for replay.
5. **`tolokaforge.core.model_client`** / **`tolokaforge.core.model_policies`** modules deleted — every concept moved into `tolokaforge.core.llm.*`. Update imports accordingly.
6. **`in-process` runtime mode removed.** Docker is now the only supported runtime (`runtime: "docker"`). All tool execution is routed through the containerised executor service. Existing configs that specify `runtime: "in-process"` must be updated to `runtime: "docker"`.

### Fixed

1. **P1 / GPT-5.5 Decimal tool-call 500s** — `StrictSchema` now strips RE2-incompatible `pattern` + `format` keys and collapses Pydantic's `Decimal` `anyOf{number, string+pattern}` idiom to `{type: number}`. Four OTS domains that scored 0.000 on gpt55 now return valid tool calls.
2. **P2 / Qwen dict-map stringification** — new `qwen` preset wires `schema_sanitizer: strict` + `response_policy: array_dict_map` + `prompt_policy: dict_map_hints`. `qwen/*` and `qwen3*` now handle `Dict[str, T]` parameters correctly.
3. **P3 / Claude 4.7 ignores `reasoning: medium`** — new `anthropic_claude_4_7` preset emits canonical litellm `thinking={"type":"enabled","budget_tokens":N}` kwarg + drops `temperature` / `top_p` / `top_k` when thinking is active.
4. **P4 / Anthropic thinking blocks dropped across turns** — new `ReasoningCodec` abstraction captures full `{type, thinking, signature}` blocks on extraction and splices them back via `thinking_blocks` on assistant message dicts for interleaved-thinking replay.
5. **P5 / user-simulator prompt never persisted** — new `Trajectory.user_system_prompt` field captures the full simulator system prompt on first turn.
6. **P6 / effective tool schemas never persisted** — new `results/tools_schemas/<task_id>__<model_id>.json` sidecar dedup'd per `(task, model)` via filename.
7. **P7 / cache + reasoning token counters lost** — new `Usage` dataclass + `UsageExtractor` reads every normalised litellm field: `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, plus `provider_raw` for forensics. OpenRouter-routed Anthropic caching now surfaces correctly — reads `prompt_tokens_details.cache_write_tokens` / `cache_read_tokens` as a fallback when top-level Anthropic fields are zero.
8. **P8 / no `cache_control` markers on Anthropic calls** — new `AnthropicEphemeralCache` automatically marks the last system-prompt content block + last tools entry with `cache_control: {type: ephemeral}` (5-minute TTL, Anthropic default).
9. **P9 / `reasoning: medium` abstraction leak** — new `ReasoningConfig(mode, budget_tokens, effort_hint, display)` provides explicit per-provider routing. Single in-repo legacy config migrated; external configs rebase in lockstep.
10. **P10 / no shared per-provider integration scaffolding** — new `tests/integration/llm/` with `Capability` enum + `ModelCertificate` registry; per-capability tests auto-skip with explanatory messages based on each model's declared certificate. See [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md).

### Added

1. `tolokaforge/core/llm/` package with seven Protocol-driven policy modules:
   - `reasoning.py` / `reasoning_codec.py` — structured thinking-block extraction + replay
   - `schema_sanitizer.py` — `ToolSchemaSanitizer` with RE2 post-condition
   - `cache_policy.py` — `CachePolicy` with ephemeral-cache implementation
   - `usage.py` — `Usage` dataclass + `UsageExtractor` + field-wise `__add__`
   - `params_policy.py` / `content_policy.py` / `response_policy.py` — pre-existing classes ported
   - `capabilities.py` — `ModelCapabilities` with all seven policy slots
   - `presets.py` — preset registry with reverse-lookup + fingerprint helpers
2. `tolokaforge/core/output/artifacts.py` with `TrialArtifactWriter` Protocol + `FileArtifactWriter` + `model_id_slug`.
3. [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) — single authoritative reference for the new package.
4. [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md) — six-step contributor guide for adding a new model / provider.
5. `anthropic/claude-opus-4.8` registered in pricing catalog, model presets (version-specific `anthropic_claude_4_8` preset ordered before generic `anthropic` for first-match-wins routing), and integration `ModelCertificate` (live-certified 2026-05-29; promotes `DICT_MAP_TOOL_CALL` + `DECIMAL_FIELD_TOOL_CALL` to `required` versus 4.6/4.7's `known_unsupported`).

### Changed

1. `litellm` version range set to `>=1.83.14,<2.0.0` (was `>=1.0.0`). Minimum version required for the canonical `thinking={}` kwarg and `thinking_blocks` first-class assistant-message field.
2. `task.yaml.model_config.<role>.resolved.*` block now records `{effective_preset, schema_sanitizer, prompt_policy, content_policy, response_policy, reasoning_codec, cache_policy}` for analytics-level config-drift detection. See [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md) § `task.yaml`.

### Traceability

Every P# in [`plans/llm_reasoning_and_observability_fix.md`](plans/llm_reasoning_and_observability_fix.md) maps to a closed fix. Integration evals (Stage 10) require live API keys and are run manually — see [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md) for the capability suite.

## v0.2.0 (2026-02-25)

### Added

1. `evaluation.task_packs` support across Docker runtime.
2. Multi-root mock-web routing via `TASKS_DIRS`.
3. Docker task-pack mount planning and smoke validation scripts.
4. Public benchmark examples across all OSS v1 benchmark types.
5. Tiered CI pipeline (PR smoke, nightly/full, release gate).
6. Public export verification tooling:
   - `scripts/release/prepare_public_export.sh`
   - Public export verification scripts
   - `scripts/tests/public_export_smoke.sh`

### Changed

1. Public examples were upgraded to non-placeholder structure with stronger grading and fixtures.
2. CI summary thresholds now enforce completion-rate in mock smoke runs and configurable pass-rate in release gating.
3. Mock-web static path resolution now supports multi-page task-local `www/` layouts.

### Security

1. Public export flow now strips internal-only integrations and scans for forbidden internal URL patterns.
