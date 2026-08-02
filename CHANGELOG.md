# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Changed

- **runner**: **a run refuses to start against a pack whose grading cannot be graded, naming every offending task.** Before it schedules the first trial — and at `tolokaforge prepare`, so a distributed enqueue is rejected once rather than by every worker identically — the orchestrator makes one pass over every selected task and puts its grading block through the same predicate `tolokaforge validate` applies. That covers the authoring rules checked against the task's tools *and* every migration rejection the typed grading blocks carry: a removed `state_checks.env_assertions` / `db_hash_check` key, and the free-text `rubric` / `output_schema` / per-task `model_ref` shapes, were previously only rejected while artifacts were written — the last phase of a trial the author had already paid for (#696). The abort lists **every** offending task rather than the first, because an author fixing a run's packs wants the list. The pass resolves each task's wire description once and keeps it, so the trials that follow reuse it; a task naming an adapter the host has not installed is rejected in the same pass. `evaluation.grading_validation.fail_on` (a new optional block, default `advisory`) names the least severe finding class that is fatal — `advisory` fails on errors and advisories, `error` on errors alone — and what the gate could not check is logged and fails nothing under either. An existing run config is unaffected — but a run whose pack was already mis-authored now aborts at startup instead of grading with a check that silently decided nothing. Note that `evaluation` is `extra="ignore"`, so a misspelled *block* name (`grading_validaton:`) is dropped without a word and the defaults stand; the block's own fields are `extra="forbid"` (#679, #696)

- **grading**: **a grading block that names a tool the task does not have is now an authoring error, heard before a trial is paid for.** Three shapes were reproduced end to end and all three charge the author's typo to the agent or to nobody: `tool: { equals: http_reqest }` in a `present` matcher scores the component `0.0` with `present is unmatched: match selected no event`, byte-identical to a genuine agent failure; the same typo under `absent` scores `1.0` against a timeline where the tool *was* called, so the check can never fail; and an uncompilable `regex` raises `re.error` out of the evaluator at grade time, which core lets propagate and the runner folds into a failed grade response — losing the trial, not the constraint. `tolokaforge validate` and (from the pre-run gate) a run now check the block against the tools the task gives its actors: **errors** for a tool name outside the declared set (in `trace_checks` matchers and in `transcript_rules.tool_expectations`), for an argument name outside the properties of a tool whose schema forbids extras, for a pattern that does not compile, and for `state_checks.hash.expected_state_hash` declared under a falsy `hash.enabled` — a confirmed silent no-op, since both substrates read the flag before the hash; the flag is read for truth rather than for `true`, because core branches on its truthiness and the runner coerces it, so `enabled: 1` grades and loads; an **advisory** for an argument name outside the properties of a tool whose schema *permits* extras, which an MCP schema always does, so hard-failing would enforce a claim the schema does not make. What the schema cannot answer is reported as **unchecked and never fails anything** — a separate channel, not a third severity, so the gate has no false-reject mode: nothing reads it to decide whether to raise, and the CLI prints it beside the task because a gate that could check nothing must not read as a clean bill of health. An `args` path is checked at its **first segment only** (`json.q` on `http_request` is checked at `json` and stops, because `json`'s schema declares no properties); descending further is **#765**. A tool named by `regex` rather than by `equals` / `in_` produces no finding — a pattern names a set, not a token. Measured on the shipped corpus: all **57** packs under `examples/**` and `tests/data/tasks/**` that ship a `grading.yaml` produce **zero errors and zero advisories**, so nothing that grades today stops loading (#679)

- **grading**: **an ordering constraint written over one matcher is rejected at load unless some trajectory can decide it.** Both sides of a `before` carrying the same matcher, or an `absent_before` / `absent_between` forbidding the very events its window is measured from, is usually a constant — nothing follows the last of a matched set and nothing precedes the first — so the check passes or fails every trial whatever the agent did, which is the same defect as an unsatisfiable count bound. Measured cell by cell against the evaluator at zero to four matching calls: of the 38 quantifier combinations these shapes admit, **ten** still say something and are kept — `before` and `immediately_before` with `left ∈ {first, any}` and `right ∈ {last, any}` ("the events occur at least twice", except `immediately_before` `first` before `last`, which reads exactly twice), `absent_before` anchored `last` ("the events occur once"), and `absent_between` from `first` to `last` ("exactly twice"). Of the other 28, twenty-seven are false at every trajectory; the twenty-eighth — `absent_before` forbidding its own anchor, anchored `first` — is **not** a constant but decides exactly what `present` decides, so it is rejected as `present` written the long way round rather than as a check no agent can move. All 28 are load errors naming the shape that would express the intent. No shipped pack writes one (#679)

- **cli**: **`tolokaforge validate` exits non-zero, and validates the task a run would load.** The command counted failures, printed `N valid, M invalid` and returned `0` regardless — so the four CI steps that run it (`ci.yml:126, 198, 302` and `release-gate.yml:55`) could never fail a build, and every "fails `validate`" contract in the docs was unenforceable. It now exits `1` when any matched task fails to load, after printing the same per-task lines and summary, and **`1` when the glob matches no file at all**, naming the pattern: a pattern selecting nothing validates nothing, which is exactly the shape a CI glob drifts into (#764). **A script or CI step that treated a failing `validate` as success now fails.** Measured on the shipped corpus: `examples/**/task.yaml` → 28 valid / 2 invalid, exit `1` (the two are the `terminal_bench` pair, which declare no `task_id` / `description`); both CI globs — `examples/native/*/dataset/**/task.yaml` (23/0) and `tests/data/tasks/wire_probes/dataset/**/task.yaml` (20/0) — stay clean and exit `0`, so no lane reddens. Each task now also loads **under its enclosing project**: `validate` resolves the `project.yaml` above the task and layers its `task_defaults` beneath the task's own fields, the same layering the orchestrator applies, so the object validated is the one the run loads rather than a project-less reading of it — measured, that changes the effective config of 18 of the 28 example tasks and breaks none. `project.default_environment` is deliberately *not* layered: it binds into a `TaskDescription`'s `EnvironmentManifest` and `validate` builds none. A `project.yaml` that fails to load fails the tasks beneath it, naming the project file, and the rest of the glob is still validated. **`make validate` gains a directory guard** — task packs are cloned separately, so with `TASKS_DIR` (default `tasks`) absent *and* `TASKS_GLOB` still the default derived from it, the target prints a skip reason and exits `0` instead of failing on an empty glob. Naming a `TASKS_GLOB` runs it whatever `TASKS_DIR` holds, so pointing the target at a pack elsewhere is never silently skipped. The dev MCP's `validate_tasks` answers for the same default and skips the same case (#679)

### Fix

- **grading**: **an attempt whose grading refuses is counted and keeps its bundle instead of vanishing from the run.** `RunnerRPCTrialGrader.grade` raises `GradingFailedError` when `GradeTrial` returns no verdict; the conductor's grading phase catches it, records the reason on the new `Trajectory.grading_error`, and lets the trial finish its normal path. The attempt reaches `total_trials` and `measured_trials`, stays out of `scored_trials`, and its `status` / `termination_reason` still describe how the trial itself ended — grading's failure rewrites neither. Previously the exception escaped the conductor, so `_write_artifacts` never ran and the trajectory never reached the run's results: **a two-trial task with one grading failure reported `total_trials: 1` and a perfect `success_rate: 1.0`, and wrote no `trials/` directory at all.** The same task now reports `total_trials: 2`, `measured_trials: 2`, `scored_trials: 1`, `avg_score: 1.0`, `success_rate: 0.5`, `pass@1: 0.5` — every rate deflates visibly and boundedly, never inflates. **The attempt is also no longer retried**: it terminates normally, and retryability reads the trajectory's own status and reason. A grading failure a second attempt would have got past is therefore recorded ungradeable on the first — the price of never fabricating a verdict and never counting one attempt twice. Two compatibility surfaces move together: `trajectory.yaml` gains a `grading_error` key (`null` on every trial that graded or was correctly not graded), and the per-trial bundle stamp `metrics.yaml.schema_version` goes **2 → 3**. A v2 consumer could read an absent `grade.yaml` as "the infrastructure aborted this trial"; under v3 that inference is wrong, because an ungradeable trial has none either — `grading_error` is the discriminator. `Trajectory` also rejects a value carrying both `grade` and `grading_error` at construction and `model_validate`; previously written aggregates and bundles keep their values and are stale by design (#720)

- **metrics**: **an ungradeable trial is now visible as *ours*, with its own class, its own count and its own row.** `TrialOutcomeClass` gains a fourth member, `UNGRADEABLE`, which `classify_trial_outcome` reads from `Trajectory.grading_error` **before** the `(status, termination_reason)` pair and unconditionally — a refusal is typed evidence that *we* failed, while exclusion from the denominator is earned by typed evidence that the provider or the substrate killed the trial, so no termination reason buys a refused trial out of the numbers. Three compatibility surfaces move together. `per_task_metrics.json` / `aggregate.json` / `metadata_slices.json` gain an **`ungradeable`** count, sitting inside `measured_trials` exactly as `harness_errors` does; a trial is classified once, so `0 <= harness_errors + ungradeable <= measured_trials`. `outcomes_by_reason` gains a **new key form**: an ungradeable trial's row is keyed `ungradeable_<reason>` (`ungradeable_agent_done` for the common case) rather than by the reason alone, because it terminates the way a graded trial does — measured, one graded and one ungradeable `agent_done` trial shared a single row of `count: 2` whose `class` was whichever trial the loop saw last, which destroyed the split the row exists to preserve. And `aggregate.json`'s `schema_version` goes **2 → 3**: for one and the same run a v2 file and a v3 file report different `total_trials` and different rates (measured: 1 vs 2, `success_rate` 1.0 vs 0.5), which is the identical different-denominators situation that earned the 1 → 2 bump, and a consumer switching exhaustively on `class` now meets a fourth value. **Rates move downward** on any run that hit a grading failure — the attempt is a non-pass, not an absence — so a dashboard that dips on the day this lands is reporting a grading regression it was previously blind to. Previously written aggregates keep their values and are stale by design; the stamp makes the generation machine-detectable. `failure_attribution.json` also stops blaming the model for our grading bug: such a trial is `failure_class: grading_failure`, `deterministic: true`, with the cause as evidence, where it previously fell through to `model_reasoning` at `confidence: 0.5` (#720)

- **dx**: **the live panel tells the truth about a trial that was never graded.** A completed trial's row in the left pane now carries its verdict — `pass`, `fail`, or `n/a` — three pairwise-distinct renderings, so a trial grading could not judge no longer reads as one the agent passed. Previously the pane rendered `✓` for **every** completed trial whatever the verdict, and stored `binary_pass` and `score` without ever drawing them: an ungraded, non-retryable trial looked like a pass, and the fabricated `binary_pass=False` the orchestrator handed the display for any trial with no grade was wrong and invisible at once. The call site now passes what the trajectory actually holds, and `RunDisplayEvents.trial_completed` / `LiveRunDisplay.trial_completed` widen `binary_pass: bool` → `bool | None` to carry it. `RunDisplayEvents` is a documented Protocol seam ([ADR-0019](docs/adr/0019-front-end-plugin-namespace.md)): the widening is source-compatible for callers and **not** for a third-party implementation that narrows the parameter to `bool` — such an implementation must widen its own signature. The in-tree `_NullRunDisplayEvents` takes `**_` and is unaffected. Closes #714 (#720)

- **runtime**: materialisation now bind-mounts the host docker socket (`/var/run/docker.sock`) into the runner service whenever a task routes a shipped tool through the compose variant (`tools.agent.<tool>.service`), on the same trigger that bakes the docker CLI into the runner image. The compose-variant `bash_session` / `str_replace_editor` wrappers `docker exec` from the runner into a sibling service; the CLI was present but the socket was not, so every exec failed to reach the daemon and the tool lifecycle surfaced `bash session failed to become ready` at trial-register time. Both per-trial and shared task-declared-stack backends inject the mount; the injection is idempotent, so a pack that already declares the socket volume is left unchanged
- **runner**: the persistent `bash_session` shell no longer recurses to a `RecursionError` when its backing process cannot stay alive. `_PtyBashSession.run` treated an empty read (the shell pipe closing — the process exited) as a timeout, routing a dead session into `_terminate_runaway`; for the compose engine (`docker exec` into a service container) that path reopens the exec and re-runs the sentinel, which reads EOF again and recurses without bound (one forked `docker exec` per level) whenever the target container is not running — surfacing at trial start as `Tool lifecycle start failed: maximum recursion depth exceeded`. EOF is now handled distinctly from a timeout: the session is closed and the failure surfaces cleanly (e.g. `open` raises `bash session failed to become ready`)
- **runner-client**: `GrpcRunnerClient.health_check` now accepts both `healthy` and `degraded` runner statuses (per `docs/GRPC_PROTOCOL.md` § HealthCheck) as reachable-for-connect purposes. The prior strict `status == "healthy"` check made every trial pack without a `db-service` fail the 30-attempt connect loop even though the runner's `HealthCheck` RPC was successfully answering. Only `unhealthy` and `RpcError` remain as fail states (#801)

### Feat

- **grading**: **`trace_checks` grades genuinely alternative routes, and a constraint can be a check that must hold rather than a share of the score.** Two new authorable keys on the `trace_checks` block. **`alternatives`** declares **two or more** named routes (`id`, `description`, its own `constraints`); each is scored over its **decision set** — the shared `constraints` plus that route's own, normalised within the set, so routes need no weights against each other — and the component is the **highest-scoring route's**, with the winner recorded. A tie goes to a route whose gate shut and otherwise to the first declared: preferring the gated route can only ever shut a component and never rescue one, and without it the trial's verdict would turn on which of two equal-scoring routes the author wrote first. This is what `any_of` structurally cannot express: `any_of: [A_step1, B_step1]` beside `any_of: [A_step2, B_step2]` passes an agent that did half of each, which is neither route. A **single** path is the flat form written the long way round and is a load error, as are a block declaring neither key, an id repeated anywhere in the block's one id space (paths and every constraint list share it, because an id is how the grade and the pre-run gate address a sub-check), a `weight` beside a gate, `on_missing: pass` beside a gate (it would open the gate on every trial whose anchor matched nothing, and a gate carries no share for the policy to save a second charge on), and a route whose decision set has no scored member while another route's does (a constant `1.0` standing in front of every scored sibling — that gate belongs in the shared `constraints`). **`severity: gate`** marks a constraint that is not scored and must hold: it enters neither the numerator nor the denominator of the weighted fraction, a tripped gate takes the component to `0.0` and **fails the trial on both substrates** independent of `pass_threshold`, and the grade names the gates that shut and the route the score came from. The core engine had no gate path at all before this — the rubric judge is runner-only — so this is the first gate it honours. **This is the judge's required-criterion gate reused exactly**, not a second semantics: excluded from the average, the failed-id list, the component zeroing, the forced `binary_pass`, and the all-gates collapse (`1.0` when every gate held, `0.0` otherwise) — which is **not** conditional on `alternatives`, since a flat block of nothing but gates reaches it too. `tests/canonical/test_gate_semantics_parity.py` drives `aggregate_rubric` and the trace fold through one shared answer table and fails naming the cell if they disagree; no shared helper is extracted, because the trace fold's weighted fraction is held to one division and no branch while the judge's must raise on a non-positive denominator (**#771**). **An undecided gate trips it.** Undecided is not a pass in the agent's favour anywhere in this vocabulary, and a gate that opened there would be *weaker* than the scored constraint it replaced. The consequence is sharpest on a bundle re-graded without its tool-call record, which cannot read `status` or `executor` (#682), so a gate reading either fails every re-graded trial — write gates over evidence the message view carries and keep `status` / `executor` for scored constraints, where the same limit costs a weight rather than the trial. **A path gate is a *process* gate**: it constrains how *that* route must be walked and is consulted only on the route that won, so **a gate that must hold whatever route the agent took belongs in shared `constraints`**, where it is in every decision set. The argmax deliberately runs over every route including gated ones, so a route cannot escape its own gate by scoring at or below a clean sibling; the escape that remains one level up — trip a route's gate *and* sandbag that route so a clean one strictly outscores it — is real, ships documented, and is pinned by a characterisation test rather than claimed closed. **Wire**: `TraceConstraintResult` gains `string severity = 7`, and `Grade` gains `TraceChecksSummary trace_checks_summary = 11` — `winning_path`, `gate_failed`, `failed_gate_ids`, and one `TracePathResult` per declared route carrying that route's own score, never zeroed, so the max the component took is auditable from `grade.yaml` alone. A message rather than four scalars, because proto3 scalars have no presence and a `gate_failed` of `false` decoded from a runner predating the field is a gate silently opening; a summary the host cannot read **fails the grade parse** rather than being dropped. Both fields are written on every graded trial, matching their unconditionally-serialised sibling `trace_check_results`. **These are new keys on `extra="forbid"` runner-side models, so an engine of this release requires a runner image built from it for any pack declaring either** — `RegisterTrial` rejects an older image — while core's `extra="ignore"` means an older engine ignores them. `cache_debug` is the only shipped pack that declares them (see the entry below; scores on that pack are not comparable across the change) and no other pack changes verdict. Two adjacent limits are declared rather than closed: a `GradingKey` carries one `runner_field`, so a constraint kind written only inside a route falls outside the per-kind ledger's "populated ⇒ accounted" guarantee, which the manifest entry now says (**#772**); and `lot_ops_01` still passes an agent that double-posts the corrective action, a one-line application of this feature (**#773**) (#680)

- **grading**: **`cache_debug` grades two genuinely alternative diagnostic routes, and can no longer be passed by mutating the order it was asked to diagnose.** `examples/native/multi_service_cache_debug/dataset/tasks/cache_debug/grading.yaml` is the first shipped pack to declare `trace_checks.alternatives` and `severity: gate`. **Scores on this pack are not comparable across this change.** The whole `transcript_rules` block is **deleted** and the weights move from `{state_checks: 0.3, transcript_rules: 0.2, llm_judge: 0.5}` to `{state_checks: 0.25, trace_checks: 0.25, llm_judge: 0.5}`. Both were measurable defects on the shipped pack, not hypotheticals. `required_actions` can express only one conjunction of exact-URL matches, so the pack had to pick a single diagnostic route and demanded a cache-admin read — an agent that diagnosed correctly by comparing `GET /orders/4021` against `GET /orders/4021/source`, a derivation the pack's own rubric reference calls valid, scored **CORE `(0.9333, True)` / RUNNER `(0.95, True)`**, docked on both substrates for the route it chose (the two numbers differ only by the aggregation divergence #685 owns: core multiplies action × comm × legacy for 2 of 3 required actions, the runner takes 3 of 4 rule rows). Both comparisons the rubric reference names are now declared as `alternatives` and each scores `1.0` with the winning route recorded. Separately, `POST /orders/4021` is reachable with the agent's own `http_request` tool, and an agent that "fixed" the symptom that way while writing a correct note scored **`(1.0, True)`** — full marks for a forbidden action on a diagnose-only task; the new shared `no_status_was_written` gate takes the component to `0.0` and fails the trial. The gate is **shared rather than per-route** because it holds whichever route the agent took, which is the authoring rule `docs/GRADING.md` now points at this pack to demonstrate. The pack's turn ceiling leaves grading with `transcript_rules`: `task.yaml` declares `max_turns: 20` and the loop iterates that budget, so the deleted `transcript_rules.max_turns: 20` was an always-pass check. `tests/canonical/test_example_pack_grading_corpus.py` holds each of the six declared checks to failing on exactly one wrong-process trajectory and no other, and `tests/integration/test_cache_debug_end_to_end.py` no longer requires the agent to reach both service hosts — after this change a correct agent may never open the cache inspector (#680)

- **grading**: **`grading.yaml` gains a `trace_checks` block — a declarative vocabulary for what the agent did and in what order.** A constraint carries an `id`, a `description`, an optional `weight` and `on_missing`, an optional inclusive `within` turn window, and exactly one of **ten** constraint kinds under `require`: `present`, `absent`, `count`, `before`, `immediately_before`, `absent_before`, `absent_between`, `all_of`, `any_of`, `negate`. Matchers select timeline events by a required `kind` (`tool_call`, `tool_result`, `assistant_message`, `user_message`) and carry `ValuePredicate`s over the fields that kind actually has — `tool`, `executor`, `args` (nested, dotted paths), `status`, `result`, `text` — where a predicate is the **conjunction** of the fifteen operators it declares (`equals`, `equals_ci`, `contains`, `contains_ci`, `not_equals`, `regex`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `len_gt`, `len_gte`, `exists`), deliberately unlike `state_checks.jsonpaths`, which rejects a second operator. This expresses what `transcript_rules` structurally cannot: ordering, scoped negation, non-equality argument predicates, nested argument paths, counting, and a call's status or outcome. **The block is authorable and validated here; the entry above is where both substrates score it.** `latency_seconds` is **not** matchable: wall time is not compared across substrates, so grading must not depend on it. A `result` predicate is admitted only beside a `status` predicate reading exactly `{equals: success}`, rejected at load naming **#717** — a successful call's result text is byte-identical across substrates and canonically pinned, a failed call's is not, so matching failure text would grade differently on the two substrates; assert `status` instead. **`tolokaforge validate` constructs the whole block**, so every shape that could only ever select nothing is heard before a trial is paid for: an unknown operator, kind or matcher field (`extra="forbid"` throughout); zero or two kinds under one `require`; a predicate declaring no operator; a predicate on a field the kind never carries; `immediately_before` without an explicit `among` (there is no default — events interleave inside a turn, so `tool_calls` cannot express confirm-before-acting and `events` cannot express two consecutive calls); `any`/`all` on a window anchor, whose domain is restricted to `{first, last}` because over an interval the other two collapse onto them; `on_missing` on `present`, `absent` or `count`, whose verdict is itself the match; `count` with no bound or with `min > max`; a duplicate constraint `id`; a `within` window that is inverted or restricts nothing; a `weight` that is not a positive finite number, because a zero weight is a declared check contributing to neither side of the fold and "evaluated but not scored" is `severity: gate`; and a block that is not a mapping at all (a constraint indented directly under `trace_checks:` makes it a list), rejected naming the file, the key and the shape received. **This is a new authorable key on both `extra="forbid"` runner-side models, so an engine of this release requires a runner image built from it for any pack declaring the block**; core's `extra="ignore"` means an old engine ignores it, and no shipped pack declares one. `TraceEventKind` moves to the leaf `tolokaforge/core/grading/trace_event_kind.py` and is re-exported from `trace_timeline`, so the timeline and the matchers that select on it name one enum rather than two that can drift (#678)

- **grading**: **`trace_checks` grades genuinely alternative routes, and a constraint can be a check that must hold rather than a share of the score.** Two new authorable keys on the `trace_checks` block. **`alternatives`** declares **two or more** named routes (`id`, `description`, its own `constraints`); each is scored over its **decision set** — the shared `constraints` plus that route's own, normalised within the set, so routes need no weights against each other — and the component is the **highest-scoring route's**, with the winner recorded. A tie goes to a route whose gate shut and otherwise to the first declared: preferring the gated route can only ever shut a component and never rescue one, and without it the trial's verdict would turn on which of two equal-scoring routes the author wrote first. This is what `any_of` structurally cannot express: `any_of: [A_step1, B_step1]` beside `any_of: [A_step2, B_step2]` passes an agent that did half of each, which is neither route. A **single** path is the flat form written the long way round and is a load error, as are a block declaring neither key, an id repeated anywhere in the block's one id space (paths and every constraint list share it, because an id is how the grade and the pre-run gate address a sub-check), a `weight` beside a gate, `on_missing: pass` beside a gate (it would open the gate on every trial whose anchor matched nothing, and a gate carries no share for the policy to save a second charge on), and a route whose decision set has no scored member while another route's does (a constant `1.0` standing in front of every scored sibling — that gate belongs in the shared `constraints`). **`severity: gate`** marks a constraint that is not scored and must hold: it enters neither the numerator nor the denominator of the weighted fraction, a tripped gate takes the component to `0.0` and **fails the trial on both substrates** independent of `pass_threshold`, and the grade names the gates that shut and the route the score came from. The core engine had no gate path at all before this — the rubric judge is runner-only — so this is the first gate it honours. **This is the judge's required-criterion gate reused exactly**, not a second semantics: excluded from the average, the failed-id list, the component zeroing, the forced `binary_pass`, and the all-gates collapse (`1.0` when every gate held, `0.0` otherwise) — which is **not** conditional on `alternatives`, since a flat block of nothing but gates reaches it too. `tests/canonical/test_gate_semantics_parity.py` drives `aggregate_rubric` and the trace fold through one shared answer table and fails naming the cell if they disagree; no shared helper is extracted, because the trace fold's weighted fraction is held to one division and no branch while the judge's must raise on a non-positive denominator (**#771**). **An undecided gate trips it.** Undecided is not a pass in the agent's favour anywhere in this vocabulary, and a gate that opened there would be *weaker* than the scored constraint it replaced. The consequence is sharpest on a bundle re-graded without its tool-call record, which cannot read `status` or `executor` (#682), so a gate reading either fails every re-graded trial — write gates over evidence the message view carries and keep `status` / `executor` for scored constraints, where the same limit costs a weight rather than the trial. **A path gate is a *process* gate**: it constrains how *that* route must be walked and is consulted only on the route that won, so **a gate that must hold whatever route the agent took belongs in shared `constraints`**, where it is in every decision set. The argmax deliberately runs over every route including gated ones, so a route cannot escape its own gate by scoring at or below a clean sibling; the escape that remains one level up — trip a route's gate *and* sandbag that route so a clean one strictly outscores it — is real, ships documented, and is pinned by a characterisation test rather than claimed closed. **Wire**: `TraceConstraintResult` gains `string severity = 7`, and `Grade` gains `TraceChecksSummary trace_checks_summary = 11` — `winning_path`, `gate_failed`, `failed_gate_ids`, and one `TracePathResult` per declared route carrying that route's own score, never zeroed, so the max the component took is auditable from `grade.yaml` alone. A message rather than four scalars, because proto3 scalars have no presence and a `gate_failed` of `false` decoded from a runner predating the field is a gate silently opening; a summary the host cannot read **fails the grade parse** rather than being dropped. Both fields are written on every graded trial, matching their unconditionally-serialised sibling `trace_check_results`. **These are new keys on `extra="forbid"` runner-side models, so an engine of this release requires a runner image built from it for any pack declaring either** — `RegisterTrial` rejects an older image — while core's `extra="ignore"` means an older engine ignores them. `cache_debug` is the only shipped pack that declares them (see the entry below; scores on that pack are not comparable across the change) and no other pack changes verdict. Two adjacent limits are declared rather than closed: a `GradingKey` carries one `runner_field`, so a constraint kind written only inside a route falls outside the per-kind ledger's "populated ⇒ accounted" guarantee, which the manifest entry now says (**#772**); and `lot_ops_01` still passes an agent that double-posts the corrective action, a one-line application of this feature (**#773**) (#680)

- **grading**: **`cache_debug` grades two genuinely alternative diagnostic routes, and can no longer be passed by mutating the order it was asked to diagnose.** `examples/native/multi_service_cache_debug/dataset/tasks/cache_debug/grading.yaml` is the first shipped pack to declare `trace_checks.alternatives` and `severity: gate`. **Scores on this pack are not comparable across this change.** The whole `transcript_rules` block is **deleted** and the weights move from `{state_checks: 0.3, transcript_rules: 0.2, llm_judge: 0.5}` to `{state_checks: 0.25, trace_checks: 0.25, llm_judge: 0.5}`. Both were measurable defects on the shipped pack, not hypotheticals. `required_actions` can express only one conjunction of exact-URL matches, so the pack had to pick a single diagnostic route and demanded a cache-admin read — an agent that diagnosed correctly by comparing `GET /orders/4021` against `GET /orders/4021/source`, a derivation the pack's own rubric reference calls valid, scored **CORE `(0.9333, True)` / RUNNER `(0.95, True)`**, docked on both substrates for the route it chose (the two numbers differ only by the aggregation divergence #685 owns: core multiplies action × comm × legacy for 2 of 3 required actions, the runner takes 3 of 4 rule rows). Both comparisons the rubric reference names are now declared as `alternatives` and each scores `1.0` with the winning route recorded. Separately, `POST /orders/4021` is reachable with the agent's own `http_request` tool, and an agent that "fixed" the symptom that way while writing a correct note scored **`(1.0, True)`** — full marks for a forbidden action on a diagnose-only task; the new shared `no_status_was_written` gate takes the component to `0.0` and fails the trial. The gate is **shared rather than per-route** because it holds whichever route the agent took, which is the authoring rule `docs/GRADING.md` now points at this pack to demonstrate. The pack's turn ceiling leaves grading with `transcript_rules`: `task.yaml` declares `max_turns: 20` and the loop iterates that budget, so the deleted `transcript_rules.max_turns: 20` was an always-pass check. `tests/canonical/test_example_pack_grading_corpus.py` holds each of the six declared checks to failing on exactly one wrong-process trajectory and no other, and `tests/integration/test_cache_debug_end_to_end.py` no longer requires the agent to reach both service hosts — after this change a correct agent may never open the cache inspector (#680)

- **grading**: **the flagship example pack grades its process with `trace_checks`, and no example pack can configure a component it never weights.** `examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/grading.yaml` is the first shipped pack to declare `trace_checks`: three constraints assert what the pack's README documented having been designed around — the **body** of the fixed-URL `POST /search` (`args.json.q`, a nested argument path `required_actions` cannot reach), the ordering `search_policy before create_case`, and `absent_before`, no `PATCH` onto the delivery ahead of the policy read. **Scores on this pack are not comparable across this change.** The weights move from `{state_checks: 0.6, transcript_rules: 0.15, llm_judge: 0.25}` to `{state_checks: 0.6, trace_checks: 0.25, llm_judge: 0.15}` — deterministic weight up, judge weight down — and the whole `transcript_rules` block is **deleted**, its three limitation-shaped `required_actions` replaced by the constraints above. **The turn ceiling is dropped from grading rather than migrated**: `task.yaml:5` declares `max_turns: 18` and the loop iterates that budget, so a nineteenth assistant generation is not producible and *no* grading check on the turn count can fail — the pre-existing `transcript_rules.max_turns: 18` was already an always-pass check, and a migrated `count: {kind: assistant_message, max: 18}` would have been the same check inflating the weighted fraction from `2/3` to `3/4` on one real failure. The budget stays enforced where it binds, in `task.yaml`. **New corpus guard**: `tests/canonical/test_example_pack_grading_corpus.py` holds all 28 example tasks to "every component the pack configures carries a declared weight" — #744's authoring-side exposure, where core drops an unweighted scored component and the runner invents a `1.0` for it. The guard reads the **effective** combine, after `resolve_effective_grading_combine` merges the project layer: over raw `grading.yaml` the same guard is red on the five `example-microservices-pack` tasks, which declare no `combine` of their own and inherit `llm_judge: 1.0` from `project.yaml`. **Docs**: `docs/GRADING.md` completes the `trace_checks` authoring guide (the fifteen operators with their numeric strictness, `contains`' recursive descent, the inexpressible `equals: null` and the absent `not_contains` / `not_regex`, the worked pack, and the eight declared limits with the issue that owns each), documents the four-operator `jsonpaths` vocabulary outside its error message, and `docs/TASKS.md` cross-references both (#678)

- **grading**: **both substrates score `trace_checks`, through one function, and `grade.yaml` says which constraint failed.** `evaluate_trace_checks` is called by the core engine's `grade_trajectory` and by the runner's `GradeTrial`, each over the timeline it already builds, so the component score does not depend on which substrate graded the trial — there is no second implementation to keep in step, and the canonical suite drives the two *integration points* against one authored pack because a substrate can reach a shared evaluator with a differently translated config, or not reach it at all. **Wire**: `Grade` gains `repeated TraceConstraintResult trace_checks = 10` (`id`, `kind`, `passed`, `weight`, `message`, `matched_positions`), and the host **fails the grade parse** on a payload it cannot read rather than dropping it — unlike the three JSON decode sites beside it (#759), nothing else records which constraint failed, and a `kind` outside this engine's vocabulary is exactly the version-skew case that must reject rather than degrade. **Output**: `grade.yaml` gains `trace_check_results`, one entry per declared constraint in declaration order, written inline because the block is small and the component score alone does not say which constraint moved it; `matched_positions` holds timeline positions resolved against `trajectory.yaml`. **A trial whose timeline carries no events leaves the component unscored** — every constraint would otherwise be answered by evidence the trial does not have — and the runner records that as a skip against each declared `trace_checks.constraints.<kind>` key, so a pack weighted entirely on `trace_checks` fails such a trial instead of passing it. **Carried contract change**: `on_missing` is now rejected at load on `present` as well as `absent` and `count`. `present` + `on_missing: pass` is an always-pass check — unmatched passes by the policy, matched passes by the constraint — and an unpoliced declaration inside the component built to remove them is a load error, not a documented caveat. No shipped pack declares `trace_checks`, so no pack changes verdict (#678)

- **grading**: **`trace_checks` is a fifth first-class grading component, and a task weighted entirely on a component that never ran now fails instead of passing.** One registry — `GRADE_COMPONENTS` in `tolokaforge/core/grading/grade_components.py` — declares each component's `combine.weights` key, its `grading.yaml` section, its core `GradeComponents` attribute and the runner's `*_score` attribute, and the seven sites that used to hand-write the component names all read it: the weighted fold on both substrates, the configured-but-unevaluated check, the wire→dict lowering, the host-side `Grade` construction, the runner's proto response, and `build_grade_reasons`. `state_checks` is the one entry declaring no runner attribute — hash, JSONPath and DB probes are folded into that slot before it exists. **The author-visible change**: `combine_grade_components({}, weights={trace_checks: 1.0})` with a `trace_checks:` section written returned `(1.0, True)` — a silent pass on nothing, where the same shape with any of the other four returned `(0.0, False)` — and now returns `(0.0, False)` like the rest. A weight with **no** matching section still passes by default; the two substrates disagree there and that is **#758**, deliberately untouched here because changing it moves existing packs. The one shipped pack that declares `trace_checks` — `helpdesk_01` — weights it and has it evaluated, so it never reaches the configured-but-unevaluated branch and no pack changes verdict. **Wire**: `GradeComponents` gains `optional double trace_checks = 5` — explicit presence rather than the `-1.0` sentinel the other four use, because proto3 scalars have no presence and a runner image predating the field would decode as `0.0`, recording a scored zero for a runner that cannot evaluate trace checks at all; the `RegisterTrial` version lock does not cover that direction, since a newer engine registers happily against an older runner. The host reads presence, so an absent field, an absent `components` submessage and the `-1.0` sentinel all reach `None`, while a present `0.0` survives as the real failing score it is (#678)

- **grading**: **`grading.yaml` gains a `trace_checks` block — a declarative vocabulary for what the agent did and in what order.** A constraint carries an `id`, a `description`, an optional `weight` and `on_missing`, an optional inclusive `within` turn window, and exactly one of **ten** constraint kinds under `require`: `present`, `absent`, `count`, `before`, `immediately_before`, `absent_before`, `absent_between`, `all_of`, `any_of`, `negate`. Matchers select timeline events by a required `kind` (`tool_call`, `tool_result`, `assistant_message`, `user_message`) and carry `ValuePredicate`s over the fields that kind actually has — `tool`, `executor`, `args` (nested, dotted paths), `status`, `result`, `text` — where a predicate is the **conjunction** of the fifteen operators it declares (`equals`, `equals_ci`, `contains`, `contains_ci`, `not_equals`, `regex`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `len_gt`, `len_gte`, `exists`), deliberately unlike `state_checks.jsonpaths`, which rejects a second operator. This expresses what `transcript_rules` structurally cannot: ordering, scoped negation, non-equality argument predicates, nested argument paths, counting, and a call's status or outcome. **The block is authorable and validated here; the entry above is where both substrates score it.** `latency_seconds` is **not** matchable: wall time is not compared across substrates, so grading must not depend on it. A `result` predicate is admitted only beside a `status` predicate reading exactly `{equals: success}`, rejected at load naming **#717** — a successful call's result text is byte-identical across substrates and canonically pinned, a failed call's is not, so matching failure text would grade differently on the two substrates; assert `status` instead. **`tolokaforge validate` constructs the whole block**, so every shape that could only ever select nothing is heard before a trial is paid for: an unknown operator, kind or matcher field (`extra="forbid"` throughout); zero or two kinds under one `require`; a predicate declaring no operator; a predicate on a field the kind never carries; `immediately_before` without an explicit `among` (there is no default — events interleave inside a turn, so `tool_calls` cannot express confirm-before-acting and `events` cannot express two consecutive calls); `any`/`all` on a window anchor, whose domain is restricted to `{first, last}` because over an interval the other two collapse onto them; `on_missing` on `present`, `absent` or `count`, whose verdict is itself the match; `count` with no bound or with `min > max`; a duplicate constraint `id`; a `within` window that is inverted or restricts nothing; a `weight` that is not a positive finite number, because a zero weight is a declared check contributing to neither side of the fold and "evaluated but not scored" is `severity: gate`; and a block that is not a mapping at all (a constraint indented directly under `trace_checks:` makes it a list), rejected naming the file, the key and the shape received. **This is a new authorable key on both `extra="forbid"` runner-side models, so an engine of this release requires a runner image built from it for any pack declaring the block**; core's `extra="ignore"` means an old engine ignores it, and no shipped pack declares one. `TraceEventKind` moves to the leaf `tolokaforge/core/grading/trace_event_kind.py` and is re-exported from `trace_timeline`, so the timeline and the matchers that select on it name one enum rather than two that can drift (#678)

- **grading**: **`transcript_rules.min_assistant_turns` is an opt-in lower bound on agent activity, so a trial that produced nothing can be failed.** `max_turns` bounds the assistant-turn counter from above only and passes vacuously on a do-nothing trial — measured, a zero-turn trial declaring `max_turns: 18` scores `1.0` on both substrates — which on a refusal-style task (expected state == initial state) passes the whole trial without the agent having acted. The new key is a **gate on the whole `transcript_rules` component**, not a sub-check inside it: unmet, the component is `0.0` on both substrates and `grade.reasons` names the bound and both counts; met, it contributes nothing at all, so a pack that satisfies it scores exactly what its other keys score. Both weaker shapes were measured and rejected — as a fifth core-side averaged bucket a failed floor scores `0.8`, the default `pass_threshold`, and as one more runner sub-check beside two passing keys it scores `0.667`, which any `pass_threshold` at or below that swallows. It counts assistant **generations**, so three tool-call-only turns satisfy `min_assistant_turns: 3`; the sharper "did the agent answer" check is #678. A declared floor is also the one transcript rule evaluated on an events-less timeline, where its siblings still record `the trial's timeline carries no events` — absence is precisely the answer this key asks for, and leaving the blanket skip in place would have re-opened the vacuous pass one level up. **This is a new authorable `grading.yaml` key on the `extra="forbid"` runner-side `TranscriptRulesConfig`, and the engine emits the field on every pack carrying a `transcript_rules:` block — as `null` when the pack declares no floor — so an engine of this release requires a runner image built from it for every such pack, whether or not the pack asks for a floor** (`RegisterTrial` rejects it otherwise); old engine + new runner is safe, and core's `extra="ignore"` means an old engine ignores the key. Every existing pack is unaffected — the field defaults to unset and no pack in `examples/**` declares it. The manifest entry claims `BOTH_SCORE_PARITY` at `DIFFERENTIAL_CANONICAL`, the only `transcript_rules` key that can. **A turn window no trial can land in is rejected at load**: a `min_assistant_turns` above `max_turns` admits no assistant-turn count, so the component would be `0.0` however the agent behaved, and `tolokaforge validate` now rejects such a pack naming both keys and both values instead of letting the run be paid for first. One shared predicate (`core/grading/turn_bounds.py`) backs both `TranscriptRulesConfig` models, so a window the engine rejects at validate time is rejected at `RegisterTrial` too. `validate` reaches `transcript_rules` at all for the first time here, and it constructs the whole block — so a floor of `0` and a misspelled key inside the `extra="forbid"` `tool_expectations` are heard there as well, and a block that is not a mapping at all (a rule key indented one level too far makes it a list) is rejected naming the file, the key and the shape received rather than reading as unset and grading every rule it was meant to carry as undeclared. **`transcript_rules.max_turns` is narrowed to `1` and above on both substrates**: a ceiling below `1` closes the turn window on its own, so every trial fails the transcript component whatever the agent did — a pack declaring one no longer loads, because it could never pass a trial. Measured: all 37 declared `transcript_rules` blocks across `examples/**` and `tests/data/**` construct cleanly under both narrowings, so the widened gate rejects nothing that loads today (#677)

- **core**: new `tolokaforge.core.health` module — reusable pattern for ordered health hierarchies. `HealthLevel` (an `IntEnum` with `UNHEALTHY < DEGRADED < HEALTHY`) + `HealthReport` (a frozen dataclass wrapping the level with `is_reachable()` / `is_fully_operational()` semantic predicates) + `HealthReport.from_status()` (single-source-of-truth mapping from protocol status strings, with unknowns mapping fail-loud to `UNHEALTHY`). `GrpcRunnerClient.health_report()` is the new primary API; `health_check()` becomes a backwards-compat facade over `health_report().is_reachable()`. The pattern replaces stringly-typed status comparisons with domain predicates on the wrapper — the mapping from protocol state to "can I use this service?" lives once, not scattered across call sites

### Feat

- **runtime**: task packs may declare `services.<name>.readiness: {kind: grpc|http|tcp}`, an optional per-service client-reachability contract (default: none — the docker healthcheck stays the only readiness signal). Every existing pack validates unchanged (#803)
- **runtime**: `PerTrialRuntimeBackend.provision` gates on host-side readiness before returning — the runner is always probed for gRPC channel-readiness on its published host port, and any service declaring `readiness:` is probed by its kind. A container that is Docker-healthy but host-unreachable (loopback-only or IPv6-only bind) now fails fast with a `ProvisionError` whose `diagnostic` names the resolved endpoint, probe outcome, and the container's actual listen addresses, instead of a downstream 30 s client-connect timeout (#803)

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
