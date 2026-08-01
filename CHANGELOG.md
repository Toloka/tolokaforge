# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Feat

- **runner**: **every tool call carries the provider's call id end to end, so a call can be joined to the result it produced.** `ExecuteToolRequest` gains `call_id` and the runner records it on `RecordedToolCall` alongside a trial-wide 0-based `sequence`, which makes two calls to the same tool with byte-identical arguments distinguishable in the recorded history — the exact case position cannot resolve, and the one idempotency and no-redundant-work checks exist to catch. A call the runner refuses before execution (unknown tool name, unparseable `arguments_json`) is now recorded too, carrying the rejection's own status: the host appends a `role: tool` error message for it either way, so a record that omitted it read as a call the agent never attempted. `EXECUTION_STATUS_TRIAL_NOT_FOUND` stays message-only — there is no trial context to record into. `call_id` is required and the runner **raises** on an empty value rather than answering with a non-success status, because a tool-shaped failure is one the agent survives and retries (#676)

- **runner**: **`RegisterTrialRequest.engine_protocol_version` locks the engine to the runner image at registration.** The engine declares `ENGINE_PROTOCOL_VERSION` (`tolokaforge/runner/protocol.py`) on every registration and the runner refuses anything below its own, naming the skew in `RegisterTrialResponse.error` — a path the orchestrator already treats as fatal. **If you run an engine older than this release against a runner image built from it**, every trial now fails at `RegisterTrial` instead of starting; rebuild the image (`make docker-build-core`) or pin a matching tag. The gate is deliberately at registration rather than per call: an old engine sends no `call_id`, and rejecting each `ExecuteTool` would reach the agent as an ordinary tool error, so it would retry until the turn budget was gone and the trial would report `status=completed` with a grade near zero and the skew visible only inside the transcript. The gate is a lower bound, so a newer engine against this image still registers (#676)

- **grading**: **one recorded-tool-call type, recorded once, in true execution order, on both substrates.** Both substrates now record every tool call as a `RecordedToolCall` carrying `call_id`, `sequence`, `tool_name`, `arguments`, `executor`, `status`, untruncated `output`, `latency_seconds` and `timestamp` — replacing four incompatible per-executor log shapes, two of which recorded no tool output at all, so `result` matching was impossible core-side rather than merely less precise. `Trajectory.tool_log` is retyped from `list[dict]` to `list[RecordedToolCall]`; it is **published Python API** via `run_trial(...).trajectory`, so a consumer reading `log["tool"]` must move to `call.tool_name` and one reading `log["success"]` to `call.status is ToolExecutionStatus.SUCCESS`. Nothing on disk changes shape — `tool_log` was never written to `trajectory.yaml` (#676)

- **grading**: **a trial's tool calls are recorded in true execution order across executors.** One `ToolCallRecorder` per trial owns the list and stamps `sequence` at append time. The three per-executor lists it replaces had no shared clock, and `TrialRunner` concatenated the agent's with the user's, so cross-executor order was destroyed by construction — order looked correct only because no code path constructs a user-side tool executor (#688), which is exactly the condition #688's fix removes. `ToolExecutor`, `UserToolExecutor` and `DockerRunnerAdapter` no longer keep history (`tool_logs` / `get_logs()` / `clear_logs()` are gone); the caller that knows the `call_id` and the executor identity records. ADR-0013 is amended accordingly. The rubric judge runs the same `ToolCallingLoop` with no recorder, so grading-time tool calls cannot enter the trial's record (#676)

- **grading**: **a tool call the in-process executor refuses is now recorded.** `ToolExecutor.execute` returned early at its not-found, invalid-argument and rate-limit checks — all before its log append — so an unknown tool name or a schema-violating argument recorded nothing while the transcript still gained a `role: tool` error. A record that omits them reads as a call the agent never attempted, which would have counted **zero** failed calls for a trial full of rejected ones. Recording at the caller makes bypassing it impossible by construction. `TOOL_NOT_FOUND` and `INVALID_ARGUMENTS` are therefore genuinely producible core-side, and `GrpcRunnerClient` no longer collapses the wire's `ExecutionStatus` to a bool, so the production docker path records `TIMEOUT` too (#676)

- **metrics**: **`metrics.yaml`'s `tool_usage.total_duration_s` is no longer `0.0` for every successful call.** Latency is measured by the recording caller instead of read from `ToolResult.duration_s`, a field that defaults to `0.0` and which the *tool* was expected to populate — no tool does. Measured: a tool that really slept 50 ms recorded `0.0`. **Recorded runs' `total_duration_s` values are therefore not comparable with new ones**; old bundles are not rewritten. The documented `tool_usage` block shape in `docs/OUTPUT_FORMAT.md` is also corrected — it listed `tool` / `count` / `success` / `fail`, and the writer has emitted `tool_name` / `call_count` / `success_count` / `error_count` / `total_duration_s` (#676)

- **security**: **tool-call arguments are recorded verbatim; the core substrate no longer rewrites `password` / `token` / `secret` / `api_key` values to `***REDACTED***`.** This is deliberate. The recorded calls are the grader's input — transcript rules match declared actions against recorded arguments — so the rewrite made a trial fail an argument match it should have passed, on the core substrate only, since the runner has never redacted. Both substrates now agree on raw. Redaction belongs at artifact-write time, where discretion protects a written artifact without corrupting what the grader reads; that move is tracked in **#694**. **Until it lands, treat a recorded run's tool-call arguments as carrying whatever the agent passed** (#676)

- **grading**: **the grade-time transcript wire carries the tool-call id, and the encoding finally has an inverse.** Every assistant and user `tool_calls` entry in `GradeTrialRequest.llm_messages_json` gains `"id"`, the provider's tool-call id, so grading pairs a call with its `role: tool` result by identifier instead of by position — ambiguous exactly where it matters, since a provider can return two calls to one tool with byte-identical arguments in a single response. Encoder and decoder now live together in `tolokaforge.core.grading.transcript_wire` (`encode_transcript_wire`, `decode_transcript_wire`, `split_leading_system_message`), which is what makes the round trip assertable; the encoder had no inverse anywhere, and that is the structural reason a dropped id went unnoticed. `decode_transcript_wire` **rejects** a payload whose `tool_calls` carry no id, naming the skew, and rejects unparseable `arguments` rather than defaulting them to `{}`. **An old engine against a runner image built from this release** produces exactly such a payload — the same pairing `RegisterTrialRequest.engine_protocol_version` already gates at registration, so this adds no new version lock; a new engine against an old runner image is harmless, since the runner's transcript renderer reads only `function.name` and `function.arguments`. What the LLM judge sees is byte-identical with and without the id. `docs/GRPC_PROTOCOL.md` described a `trajectory_json` field that does not exist and a `tool_log` record shape matching none of the real ones; its `GradeTrialRequest` section now documents the payload that actually crosses, and the proto comment no longer claims the wire carries "assistant/user text only, not tool results" — the interleaved trace, tool results included, has always been on it (#676)

- **grading**: **the trial's termination reason is on the grading wire, typed.** `GradeTrialRequest` gains `termination_reason`, carrying a `TerminationReason` value so grading can tell a deliberate finish (`agent_done`) from an exhausted turn budget (`max_turns`) — the same score means something different in each case. The field is typed for the whole enum, and the runner parses it back or **fails the RPC** naming the value and the accepted set; an unrecognised value is never coerced to "not reported", which would make a skewed engine look like a healthy trial. An empty value means the engine reported none, which is a valid state, so this adds no version lock of its own: a new engine against an old runner image has the field ignored, and an old engine against a new runner image sends nothing. The reason is grading *input*, never an author-matchable key — no `grading.yaml` field, no key-manifest entry — because a task's score must depend on what the agent did, not on how the harness or the provider happened to stop the run. `tests/canonical/test_termination_reason_reachability.py` drives a real termination path for every member of the enum and pins which of them reach `GradeTrial` (#676)

- **grading**: **both grading substrates now grade every transcript rule off one shared trial event timeline, and a trial whose two views of itself disagree fails `GradeTrial` instead of being graded.** `build_trial_timeline` joins the message view to the tool-call record by `call_id`; the runner builds it once in `GradeTrial` before any component runs, and the core `GradingEngine` builds it from the trajectory, so a rule means the same thing whichever substrate evaluates it. `evaluate_transcript_rules(timeline, rules)` and `TranscriptChecker.grade(timeline, …)` replace their `(messages, tool_history)` / `(messages, tool_log)` parameters — both are internal harness functions, not published API. **The new loud failure**: a recorded call the transcript never asked for, a `call_id` used twice, or a payload that does not decode into a transcript now returns `success=False` with the offending `call_id` in the error and **no `Grade`**, and the host raises `GradingFailedError` rather than substituting one — previously such a trial was graded around, reporting a `0.0` transcript verdict against calls that had in fact succeeded, and an unparseable payload was graded as though the trial had said nothing. A trial that left neither a conversational turn nor a tool call records a named skip (`transcript_rules.* skipped: the trial's timeline carries no events`) instead of scoring every rule `0.0`, which drops the component from the combine rather than fabricating a failure. Two verdict-affecting narrowings on the core substrate, both consequences of the timeline's declared design: a `must_contain` / `disallow_regex` phrase can no longer be satisfied by the harness's own `role: system` annotations (a termination notice is not the agent's text), and a failed call's searchable text is now the executing layer's record rather than the differently-worded `role: tool` message. Core `required_actions` / `communicate_info` still evaluate from `trajectory.messages` outside the timeline, so the two substrates read the same key from different evidence — named in `docs/GRADING.md` and tracked on #685 (#676)

- **metrics**: **an infrastructure abort is no longer recorded as a task the model failed.** A trial killed by a provider rate limit, an LLM API timeout, or a substrate provisioning failure produces **no `Grade`** — `Trajectory.grade` is `None`, no `grade.yaml` is written — and is excluded from `success_rate`, `avg_score`, `avg_latency_s`, `avg_turns`, `avg_tool_calls`, `stuck_rate` and `pass@k`. `total_trials` still counts every attempt; the new `measured_trials`, `scored_trials`, `infrastructure_aborts` (per reason), `harness_errors` and `outcomes_by_reason` keys on `per_task_metrics.json` and `aggregate.json` state the denominator alongside the rates, with `measured_trials + sum(infrastructure_aborts.values()) == total_trials`. **Anyone comparing new numbers against previously published ones is comparing different denominators**: rates move upward on any run that hit provider flakiness — measured, a four-trial task with two trials rate-limited reported `success_rate` 0.25 / `avg_score` 0.325 / `pass@1` 0.25 and now reports 0.5 / 0.65 / 0.5 for the same two real trials. Previously recorded runs *are* reinterpretable without new data, because the classifier reads only `status` and `termination_reason`, both already in `trajectory.yaml`, and aggregation is recomputed from trajectories; the numbers already written into `aggregate.json` / `per_task_metrics.json` keep their old values and are stale by design, which `aggregate.json`'s `schema_version: 2` and `metrics.yaml`'s `schema_version: 2` make machine-detectable rather than a note in this file. A task whose every trial aborted reports `null` for every rate — never `0.0` — and drops out of the run's macro averages, and the run logs a warning naming each task whose `pass@k` coverage an abort reduced. Exclusion is earned by **typed** evidence only: `classify_loop_error`'s rate-limit branch is now an exception-type check over the `__cause__` chain (`openai.RateLimitError` / `status_code == 429`) instead of a substring match, so a 429-shaped *message* with no typed exception behind it terminates as `error` and is counted. `timeout`, `api_error` and bare `error` remain counted for the same reason — the first is a declared budget a thrashing agent also hits, and the other two are text-matched — because misclassifying an agent failure as infrastructure raises every benchmark number invisibly while the reverse lowers them visibly and boundedly. `TrialGrader.grade` returns `Grade | None` (internal Protocol change). Closes #689 (#676)

- **grading**: runner-side `custom_checks` execution — a pack declaring `custom_checks.enabled: true` with a `checks.py` now has its `@init` + `@check` functions executed inside the runner container over the trial's `CheckContext` (initial/final state + transcript + task metadata). Per-check verdicts ride the wire as `CustomCheckResult` entries (`Grade.custom_checks_details`), the aggregate score fills `Grade.components.custom_checks`, and the `combine.weights.custom_checks` weight is applied to the final score alongside `state_checks` / `transcript_rules` / `llm_judge`. An unsupported `interface_version` or a broken `checks.py` module now rejects the trial at `RegisterTrial` (before the agent loop) instead of at trial end. `checks.py` is bundled into `TaskDescription.tool_artifacts` for packs with or without an MCP server. The `CheckExecutor` Protocol seam ([ADR-0012](docs/adr/0012-custom-checks-extension.md)) lifts the executor to ADR-0011 Pattern A with `CheckRunner` as the in-process production impl and `InMemoryCheckExecutor` as the test fixture; no public class was renamed. See [docs/custom_checks.md](docs/custom_checks.md) and [`examples/native/custom_checks/`](examples/native/custom_checks/) — the runnable reference pack that reconciles a customer ledger arithmetically. Closes #669, #406, #217.

### Fix

- **grading**: **`combine.method` is one closed set of three aggregations — `weighted`, `all`, `any` — declared once and rejected at load when an author writes anything else.** The two retired names no substrate ever dispatched are gone: **`method: all_pass` → `method: all`, `method: any_pass` → `method: any`**, a one-line edit the rejection message names for you. Every pack declaring one was already being mis-graded — silently folded to `all` runner-side and to a weighted mean core-side — so the two never agreed on a score. `tolokaforge validate` now rejects an unsupported method instead of accepting it silently; a typo is heard before a trial is paid for rather than at grade time or never. Because the gate constructs the whole `combine` block rather than the one field, a malformed sibling (`pass_threshold: high`, a non-numeric `weights` entry) is **also** newly rejected at validate time — intended, but not a consequence of the method change, so a pack failing for that reason is not an `all_pass` pack. A `combine:` block that is not a mapping at all — a `method:` indented one level too far reads as a list item, a method written beside the key reads as a string — is rejected naming the file, the key and the shape received; it previously passed `validate` unexamined (method included) and then failed the run inside the config merge with `'list' object has no attribute 'items'`, naming neither. `combine:` with nothing under it is unchanged: that is the absent block, and every field keeps its default. This closes the bad-**value** and bad-**shape** halves of the combine typo space; a misspelled **key** (`combine: {methd: any}`) still loads and still grades as `weighted` (#745). Zero `examples/**` packs move. Also a runner-wire change in both directions — the field's domain on an `extra="forbid"` model both narrows and widens, so a new engine sending `any` to an old runner image is rejected at `RegisterTrial` (#692, #218)

- **grading**: **the core grading engine honours `combine.method` instead of always computing a weighted mean.** Both substrates now fold through one shared `combine_by_method`, so `all` reports the weakest component and `any` the strongest wherever the trial is graded, not runner-side only. Core did not read the key at all — `combine.method` appeared nowhere in `core/grading/combine.py`, whose fold was an unconditional weighted mean, so `method` could not have mattered — which is why a pack declaring `all` scored one number under `validate` and another in production. **A pack declaring `all` or `any` therefore scores differently core-side**: under `tolokaforge validate`, in the `NativeAdapter` helpers, and on any re-grade of a recorded bundle. It converges on the runner's answer, the number production has always published. No `examples/**` pack declares either, so nothing in this repo moves; per gotcha #10 `tasks/` lives outside it, so the out-of-tree corpus cannot be declared unaffected. **The two substrates still build different component maps, and for these two methods that is a verdict flip rather than a magnitude gap**: core admits only the components `combine.weights` names, while the runner admits every component it evaluated at an invented weight of `1.0`, and `all` / `any` aggregate that map alone. Measured on `state_checks: 0.0` and `transcript_rules: 1.0` at `pass_threshold: 0.8` with only `state_checks` weighted — `any` gives `(0.0, False)` core-side against `(1.0, True)` runner-side. Declaring a weight for every component the pack scores closes that divergence — on the same trial with both weighted, all three methods answer identically on both substrates. The key manifest records `combine.method` and `combine.weights` as `BOTH_SIGNAL_PARITY` tracked by **#744** for the membership gap, and **closing #744 will not make a score-parity claim on the method true**: core produces no `llm_judge` component and cannot produce a `state_checks.db_probes` one — both `RUNNER_ONLY` by design — so on a judge- or probe-graded pack core's map is empty where the runner's is scored, and `all` / `any` aggregate that map alone (#692, #218)

- **grading**: **`any` is now a method an author can declare, and it inflates scores.** It was implemented and documented but unreachable — the runner model's `Literal` rejected it, so a pack declaring it failed to load outright. It now grades, and it reports the **strongest** component while ignoring the rest: a trial whose other declared, weighted components all scored `0.0` passes with a full `1.0`, including one that failed its state hash. Measured on components scoring `0.0` and `1.0` at `pass_threshold: 0.8` — `weighted` gives `(0.5, False)`, `all` gives `(0.0, False)`, `any` gives `(1.0, True)`. This turns a loud rejection into a working score-inflating mode; declare it only when one satisfied component is genuinely the whole objective (#692, #218)

- **grading**: **a hash-enabled pack's JSONPath assertions are evaluated against the state every other grading path gives them, so conventionally rooted `$.db.…` assertions score instead of reporting `Path not found`.** Core's pre-computed-hash branch unwrapped the final state to its `db` level before evaluating `jsonpaths`, while core's two other branches, the runner, and `grade_tau_style`'s own internal split all pass the whole state. Measured on that branch: two `$.db.widgets[…].status` assertions with exactly one satisfied scored **`0.0`** with two `Path not found` reasons where the runner and core's no-hash branch both scored `0.5`. Assertions now read the whole final environment state on every path, and the hash keeps reading the unwrapped database — the level the golden state and `compute_stable_hash` describe, so no stored expected hash moves. **In-repo movement is zero and that is not reassurance**: no fixture combined a pre-computed hash with non-empty `jsonpaths`, which is why nothing caught this. **An out-of-tree pack that combines `hash` with `$.db.…` assertions will score differently — higher, and correctly** — and per gotcha #10 `tasks/` lives outside this repo, so the corpus cannot be declared unaffected (#686)

- **grading**: **a golden replay that fails to execute is a grading error, not a state-check score of zero.** `grade_tau_style` caught every exception out of `_execute_golden_actions` — a missing initial-state file, an unloadable MCP server module, no `TOOLS` map — and returned `0.0`, which is indistinguishable from a trial whose state genuinely did not match. The replay now raises `GoldenReplayError`, the trial produces **no** `state_checks` verdict, and the run's per-trial handler records the failure instead of publishing a score; a pack declaring `golden_actions` with no `initial_state.json_db` raises the same way rather than scoring `0.0`. This is convergence, not a new rule: the runner has always answered `GradeTrial` with `success=False, error="Hash grading failed: …"` for the same condition. Returning "no hash score" instead would have been worse than the zero it replaced — the remaining source then carries the whole component, so a pack with a `weight: 0.6` hash and a half-satisfied assertion set would have moved from `0.0` to its full unweighted `0.5` and could pass on an infrastructure failure. No in-repo fixture makes the replay throw (#686)

- **grading**: **a `grading.yaml` whose `state_checks` score is undecidable is rejected at load instead of scored by an invented default.** `state_checks.hash.weight` has no default anywhere, so a pack that needs one and declares none is now refused — by `tolokaforge validate` and by the core grading config model — with the remediation in the message: *"state_checks.hash.weight is required when a hash source and a non-empty state_checks.jsonpaths are both configured — there is no defensible default. Choose one: weight: 1.0 lets the hash decide, weight: 0.0 lets the jsonpaths decide, weight: 0.5 gives them equal shares."* **The one-line author fix is to add that `weight:` line under `state_checks.hash`,** choosing which source carries the verdict. **This is a task-contract change: a `grading.yaml` that loaded yesterday can fail today.** The rejected shape is exactly `hash.enabled` on **and** `hash.expected_state_hash` or `hash.golden_actions` declared **and** `jsonpaths` non-empty **and** no `weight` — every other shape produces at most one score, so a weight there would divide nothing and is accepted. Zero in-repo packs are affected (both fixtures with two live sources already set `weight` explicitly), but per gotcha #10 `tasks/` lives outside this repo, so **the out-of-tree corpus cannot be declared clean** — and every pack this rejects was already being scored by a silently-chosen weight on at least one substrate. Two narrower changes ride along: `hash.weight` is now range-checked to `[0.0, 1.0]` wherever it is declared, including where it is inert (`weight: 2.0` previously reached the blend and returned a `state_checks` component of `-0.5`); and a weight that loads but is never consulted — the recorded tau-style shape, `weight: 1.0` beside `jsonpaths: []` — is **reported** on `grade.reasons` rather than dropped in silence, which is what keeps those bundles loadable without making the ignored key invisible (#686)

- **grading**: **the runner folds a hash verdict with JSONPath assertions by the author's `state_checks.hash.weight`, so both substrates apply one fold rule instead of two.** The runner multiplied the two scores and had no weight concept at all, while the core engine blended them — the same trial got two different components, and neither substrate's number was a rounding difference from the other's. Both substrates now call one function (`core/grading/state_composition.compose_state_checks_score`); the product expression is deleted from `runner/grading.combine_grade_components` **and** from the reported-component block in `runner/service.py`, which were byte-identical copies with no shared helper, and one `resolve_state_checks_component` now decides the slot for both — mapping the runner's `-1.0` not-evaluated sentinels, applying the existing `db_probes` precedence, then folding. **In-repo verdict movement is confined to `tests/data/tasks/shop_orders_02`** (weight `0.60`, 7 assertions), and it moves in both directions: measured, a matching hash with 5 of 7 assertions satisfied goes from `0.7143` to `0.8857`, and a **failing** hash with all 7 satisfied goes from `0.0` to `0.4000`. The second direction is the one to read twice — under the blend a trial that failed its state hash can earn partial `state_checks` credit runner-side, which is what `weight` means and what the core engine has always done. An author who wants the hash to decide outright writes `weight: 1.0`. **Runner wire change**: `StateChecksConfig` gains `hash_weight`, and because that model is `extra="forbid"` and the engine emits the field on every pack with a non-empty `state_checks:` block (as `null` when no weight is declared), **a new engine against a runner image older than this release is rejected at `RegisterTrial` for every such pack** — `make docker-build-core` is part of this upgrade. The runner also applies the same presence gate the core config applies, through the same shared predicate, so an older engine that dropped `hash.weight` is refused at registration rather than having its trial folded by a rule the author never chose. **The fold rule is shared; the inputs to it are not yet.** For a pack sourced only by `expected_state_hash`, or by `hash.enabled` with no declared source at all, the two substrates still compare the trial against different expected states, so the component still differs — measured on the second shape at `weight: 0.6` against assertions scoring `0.5`, core reports `0.5` and the runner `0.8`. The manifest says so: only `state_checks.hash.golden_actions` claims `BOTH_SCORE_PARITY`, while `state_checks.hash` and `.enabled` stay `BOTH_SIGNAL_PARITY` tracked by #741. Rejecting a malformed weight also converged: the runner refused `2.0` but silently coerced `hash_weight: true` into `1.0` and `"0.5"` into `0.5`, because Pydantic's lax coercion runs before any after-validator — both are now rejected at `RegisterTrial` as core rejects them at load (#686)

- **grading**: **a failed grading run now publishes no verdict at all.** `GradeTrial` returning `success=False` — a timeline reconciliation failure, an undecodable transcript payload, or a populated scored key the runner accounted for nowhere — made the host fabricate `Grade(binary_pass=False, score=0.0)`. A normally-terminated trial classifies `MEASURED`, so that zero entered `success_rate`, `avg_score`, `pass@k` and `binary_pass` as an agent failure grading never established — worse than the pre-timeline behaviour, where only the transcript *component* reported `0.0`. `RunnerRPCTrialGrader.grade` now raises `GradingFailedError`. **Such a trial appears in no published number**: the exception leaves the conductor's grading phase, so the trial's bundle is not written and the trajectory never reaches the run's results — `total_trials` does not count it. It is loud where it happens (logged, retried, then recorded in `run_state.json` with a `trial_failed` event). Whether it should instead count as a `HARNESS_ERROR` is an open published-numbers question, deliberately not decided here (#676)

- **metrics**: **`avg_score_micro` no longer imputes a score to a measured-but-ungraded trial.** The run-level micro rebuilt its numerator as `avg_score * measured_trials`, an identity that is false for any task holding a trial with no grade — which every `harness_error` trial is, since it never reaches `GradeTrial`. Measured: one all-ungraded task beside one scoring `1.0` reported `0.5` when the only score in the run was `1.0`; a mixed task (`1.0` + a harness error) beside a task scoring `0.0` reported `0.667` against an honest `0.5`. `per_task_metrics.json` and `aggregate.json` gain **`scored_trials`** — the measured trials that produced a grade, counted from the same list `avg_score` averages — and the micro weighs by it. `success_rate_micro` keeps `measured_trials`: an ungraded trial is not a success, and dropping it from that denominator would hide the defect. `0 <= scored_trials <= measured_trials` (#676)

- **grading**: **a re-graded recorded bundle's phrase rules read what its tools returned, so a leak in a tool result no longer passes the `disallow_regex` written to catch it.** `must_contain` and `disallow_regex` search the timeline's tool results, and a `TOOL_RESULT` event existed only where a tool-call *record* did — a view no bundle carries, since `tool_log` is not written to `trajectory.yaml`. Every tool result therefore vanished from the searchable text of any re-graded trial: measured, a `role: tool` message reading `SSN 123-45-6789 for customer 42` scored `1.0` against `disallow_regex: ['\d{3}-\d{2}-\d{4}']` and `0.0` against `must_contain: ['SSN']`. The evidence was never missing — `trajectory.yaml` keeps every `role: tool` message with its `tool_call_id` — so with no record view at all the timeline now pairs each call with that message's text, joined by id and never by position. Records win wherever they exist, so **no live verdict changes**: a normal run records every call. `records_present` still reports whether a record view was supplied, so the checks that need a record's own fields (`status`, `executor`, `latency_seconds` — every `tool_expectations` and `required_actions` sub-check) still fail by name rather than reading absent evidence. A `role: tool` message answering a call the message view never declared now raises rather than being dropped (#676)

- **grading**: **a records-less timeline now fails its tool expectations by name instead of passing every `disallowed_tools` check.** `required_tool` / `disallowed_tool` / `required_action` and the core `TranscriptChecker.check_tool_expectations` filtered on `status is not None`, which conflates a call declared on a terminating turn that never ran (correctly excluded) with a call the record view says nothing about. The second state is what **re-grading any recorded bundle** is in — `BaseAdapter.grade` → `GradingEngine.grade_trajectory` builds the timeline from `trajectory.tool_log`, which is never persisted — so a replayed bundle passed every `disallowed_tools` check unconditionally and failed every `required_tools` one. The gate is now `timeline.records_present`, per tool: a tool the message view never declared is still cleared without records, because a record can only name a declared call (#676)

- **grading**: a `tool_calls` entry missing `function`, `function.name` or `function.arguments` raises `ValueError` naming the message index and the keys present. It raised a bare `KeyError`, which is not a `ValueError` and so escaped the handler that turns a bad payload into a named `GradeTrial` failure — the operator saw `Grading error: KeyError: 'function'`, naming nothing (#676)

- **grading**: **`custom_checks` rejects unknown keys.** `CustomChecksConfig` had no `extra="forbid"` and the shared gate read `.get("enabled", False)` off the raw dict, so a mistyped `enable: true` silently disabled a scored key on **both** substrates — the trial graded and nothing in its output said the block was ignored. The gate validates before it reads, so such a pack now fails at `RegisterTrial` naming the offending config (#676)

- **docs**: `docs/OUTPUT_FORMAT.md`'s `metrics.yaml` example is parseable again. The corrected `tool_usage` block had been inserted after the wrong key, leaving the superseded `tool`/`count`/`success`/`fail` shape under `tool_usage:` and the correct entries dangling after `probe_buckets: []` — `yaml.safe_load` raised `ParserError` on the whole fenced block. Closes #716 (#676)

- **runner**: **`TerminationReason.AGENT_DONE` was unreachable on the core substrate, so a trial in which the agent correctly signalled completion was recorded as having run out of turns.** `TrialRunner._is_done` folded the text it searched to lower case but not the `###STOP###` marker it searched for, so the comparison could never succeed and **every** core-substrate trial reported `max_turns`. Both operands are now folded by the same call at the point of comparison, so the match cannot be made case-blind on one side only. **This is a data-semantics correction, not a staleness note: the termination reason recorded on previously written runs is wrong, not merely old.** Trials that emitted the marker will now report `agent_done` where they previously reported `max_turns`; recorded bundles are not rewritten, and re-reading one gives the value that was recorded, not the value the trial had. No score and no aggregate rate changes — both reasons describe a measured trial — so only the recorded reason moves (#705)

- **models**: **`ToolCall.id` must be non-empty, and this is a load-time contract on stored data as well as a check on live provider output.** `Trajectory.model_validate` runs on every recorded bundle, so a trajectory whose assistant message carries a tool call without an id is now rejected at load — by replay, re-grading and the fixture loaders — naming the tool. Every trajectory recorded by this engine carries real provider ids, so nothing in-repo is affected; a hand-edited or synthesised bundle with a blank id will need the id filled in. Live, the rejection lands at `Message` construction, the earliest possible point, and inside the turn loop it classifies to the bare `error` termination reason, which is counted and surfaced rather than silently dropped (#676)

- **grading**: **a populated scored grading key that the runner cannot evaluate now fails `GradeTrial` loud instead of no-opping.** Through the component phase the runner records which author-facing `grading.yaml` key each evaluator call accounts for, then subtracts those records from the scored keys the request's grading config populated; a remainder returns `success=False` naming each key and the runner evaluator its manifest entry expects — never a grade and never a `0.0` folded into the combine. Scope is scored checks only, so `state_checks.id_fields`, `state_checks.relaxed_validation`, `state_checks.numeric_string_fields` and `combine.*` are unaffected. Legitimate skips record a reason that now appears in `grade.reasons` rather than passing silently: no transcript messages **and** no tool history (all `transcript_rules.*`), no transcript messages (`llm_judge`), `hash.enabled` unset (the `state_checks.hash` members the hash evaluator reads — the adapter fills `golden_actions` whether or not hash grading is on), and `state_checks.hash.expected_state_hash` on every request, because the adapter translates it onto the runner config and no runner path reads it (#693). A degenerate trial still scores badly instead of erroring. `grading_method: test_execution` dispatches before the component phase and is exempt (#675)

- **grading**: **removed `state_checks.env_assertions` and `state_checks.db_hash_check`.** Neither ever produced grading signal on either substrate. `env_assertions` resolved its assertion functions from a `tolokaforge/tasks/<domain>/assertions.py` module that has never existed in this repository on any branch, so every declaration raised an ImportError that hard-zeroed the **whole `state_checks` component** — a pack declaring it was scored `0.0` regardless of what the agent did. `db_hash_check` was dropped in adapter translation and never reached the runner, and silently passed when enabled with no expected hash. Both keys are now rejected at config load (`tolokaforge validate` and the grading path) with a message naming the replacement. **If you have a pack declaring either key**: use `jsonpaths` for per-record state assertions, `hash: {enabled: true}` for whole-state comparison, and `db_probes` for substrate SQL. Removal can only *raise* an affected pack's score, since the old behaviour was a guaranteed zero. An inert declaration (`env_assertions: []` / `db_hash_check: false`) requests nothing and is ignored, so recorded trial bundles still load. **Runner-image version lock**: the runner-side `StateChecksConfig` is `extra="forbid"` and no longer declares `env_assertions`, and an engine older than this release emits that field for **every** pack carrying a non-empty `state_checks:` block — so an old engine against a new runner image is rejected at `RegisterTrial` for *every* such trial, not only for packs that declared the key. `state_checks` therefore requires engine and runner image from the same release in *both* directions. `db_hash_check` never reached the runner config, so it is not part of this lock (#675)

- **grading**: `transcript_rules.tool_expectations` now grades on the runner (production) path. The key was declared core-side and dropped in adapter translation, so `required_tools` / `disallowed_tools` produced no signal on real runs. `evaluate_transcript_rules` decomposes it into one sub-check per declared tool: a required tool must have been called with `status == "success"`, and a disallowed tool must not appear in the tool history at **any** status (an attempted forbidden call is the violation). Failing sub-checks are now named individually in `grade.reasons` instead of only being counted. **Runner-image version lock**: `tool_expectations` is declared on the runner-side `TranscriptRulesConfig` (`extra="forbid"`), so an engine emitting the key needs a runner image built from the same release — `RegisterTrial` rejects it otherwise. Old engine + new runner is unaffected. **Load-time tightening**: `tool_expectations` is now a typed `ToolExpectations` model with `extra="forbid"` nested inside the `extra="ignore"` core grading config, so a pack carrying a stray sub-key under `tool_expectations` (`required_toolz`) fails at config load instead of grading as an empty list. A misspelled *tool name* is still not detected at grade time; load-time validation against the task's declared tool set is tracked in #679 (#675)

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
