# Shared context for trajectory-analysis agents

Prepend this whole block to any single-dimension prompt in this directory before
dispatching it to a sub-agent. Fill every `{{PLACEHOLDER}}` first.

You are doing READ-ONLY trajectory analysis of a model's arena eval, one dimension
per agent, to produce a GO / No-Go plus policy-fix targets. The deterministic layer
(`automation observe-findings`) has already emitted raw stats; your job
is the interpretation it deliberately does not do. A proposed policy fix is later
proved or refuted by the resolve stage (`automation reprobe`).

## Mode (decide which one before dispatching)
Two modes share these lenses but ask different questions:
- EVAL mode (full domain eval, scored `grade.yaml`): a RESULT-FIDELITY audit. Is the
  pass/fail number a TRUE measure of the model's capability, or is it depressed by a
  non-model cause - infra, a policy/preset misconfig, an engine/code bug, or an oracle
  false-failure? Subtract every non-model cause; the residual is the genuine capability
  signal. If non-model causes are material, the raw number is not publishable as-is
  (re-run / fix / footnote / regrade).
- OBSERVE mode (integration artifact, raw `findings.json`, wire probes NON-SCORING): a
  FIXABLE-ERROR audit. Which of the candidate's failures are preset-FIXABLE (FORMATTING) so
  the resolve stage can SET or CREATE a policy, and which are genuine ceilings (no fix)? The
  emphasis is the actionable policy-fix target; `reprobe.py` then proves the fix.
In both modes INFRA is the precondition and the bucket vocabulary is identical; only the
data source and the verdict framing differ per dimension (noted in each).

## Subject
- Model: {{MODEL}}
- Engine: {{ENGINE_VERSION}}
- Eval root (BASE): {{BASE_DIR}}
- Domains (trial counts): {{DOMAINS_WITH_COUNTS}}
- Total trials: {{TOTAL_TRIALS}}
- Known-good anchors to cross-check (optional): {{ANCHORS}}

## Data layout
Full-eval layout: `BASE/<domain>/trials/<TASK>/<REP>/<file>`, plus per-domain
`BASE/<domain>/{aggregate.json, per_task_metrics.json, run_state.json, summary.md (when present)}`
and a per-task `BASE/<domain>/trials/<TASK>/task.yaml`.

Observe-artifact layout instead: `BASE/wire_probes_*/trials/<task>/<rep>/trajectory.yaml`
+ `BASE/capability/*.xml` + a pre-aggregated `BASE/findings.json`. If `findings.json`
exists, read it first: the observe stage has already counted tool-arg rejections and
infra signals there (raw, unjudged). On the observe artifact, `all_passed:false` with
`capability_ran:false` means the capability suite did not run (0 probes, an INFRA
failure such as a missing key), NOT a model failure.

Per-trial / per-task files:
- `grade.yaml` - `binary_pass` (bool), `score`, `components{state_checks, transcript_rules,
  llm_judge, custom_checks}`, `reasons`, `state_diff`. THE pass/fail + why source.
- `task.yaml` - frozen task identity + the resolved preset fingerprint at
  `model_config.<role>.resolved.effective_preset`, plus the task inputs the model was
  given. THE source for "which preset applied" and "what the task actually asked".
- `logs.yaml` - `logs[]` {level INFO/WARNING/ERROR, message, context}; the final
  "Trial execution finished" entry's context carries status/turns/tool_calls/latency_s;
  the "Starting trial execution" entry's context carries max_turns.
- `metrics.yaml` - usage {prompt/completion/reasoning/cached tokens}, per-call cost list.
- `trajectory.yaml` - full conversation: assistant content, tool_calls, reasoning blocks.
- `tools_schemas.yaml` - the SANITIZED tool schemas actually sent to the model.
- `prompts.yaml` - the system prompt as sent.
- `env.yaml` - ~230KB backend state. DO NOT read in bulk.

## Metrics (the board `(c/n)^k` metric is computed in the arena FRONTEND, not this engine)
For each task, c = passing reps, n = **measured** reps — `measured_trials` in
`per_task_metrics.json`, NOT `total_trials`. A rep the infrastructure aborted (provider rate
limit, LLM API timeout, provisioning failure) produced no grade and is out of every
denominator; the gap `total_trials - measured_trials` is exactly
`sum(infrastructure_aborts.values())`. A rep whose grading refused is the other way
around — it is inside `measured_trials`, counted as a non-pass, and reported as
`ungradeable`. Report `measured_trials`, `infrastructure_aborts` and `ungradeable`
alongside any rate, so a `null` pass@k reads as lost coverage rather than as a missing
task, and a depressed rate reads as our grading bug rather than as the model's.
- pass@1 = mean over tasks of `c/n`. The per-task `c/n` is `success_rate` in
  `per_task_metrics.json` (per-task rows live there, NOT in `aggregate.json`).
- pass@5 (ceiling, any-of-k) = mean over tasks of `1 - C(n-c,5)/C(n,5)`, tasks with n>=5.
  This is exactly the engine's `compute_pass_at_k` (`tolokaforge/core/metrics.py`).
- pass^5 (the board RANK metric) = mean over tasks of `(c/n)^5`, NOT the all-5-pass fraction.
  FALSE FRIEND: the engine's `pass_hat@k` / `pass@k` fields (in `aggregate.json` /
  `per_task_metrics.json`) are the any-of-k CEILING (== pass@5), NOT the board `pass^5`.
  Never read `pass_hat@5` as the board number; compute `pass^5` yourself as per-task
  `success_rate**5` then mean over tasks with n>=5.
- MICRO base for every rate / pp below = pool ALL tasks across ALL domains,
  task-count-weighted (a per-domain macro average is a DIFFERENT base). Always state a pp
  figure is "of the micro pass@1 gap"; never add a macro pp to a micro pp.

## Four-bucket vocabulary (map every failing trial to exactly one)
- INFRA - rate_limit/429, api_error/timeout/5xx, stuck (never finished), transport-empty
  completion (HTTP/transport returned nothing). `max_turns` is INFRA ONLY when the cap was
  too low or a provider fault consumed turns; a model that took wrong/circular actions until
  the cap is GENUINE-MODEL, and a byte-identical tool+args loop until the cap is FORMATTING
  (retry-loop). Disambiguate by: did the turns advance task state? were the repeated calls
  identical args? INFRA (and only INFRA) dirties the trust gate.
- HARNESS / TASK-DESIGN / ORACLE - grader artifact, ambiguous/unwinnable spec, set/tag
  ordering false-fail, correct refusal graded fail. Model-independent.
- FORMATTING - schema-loss/dropped-param, dict-stringify, reasoning-leak, retry-loop. The
  ONLY preset-fixable bucket. A "policy fix" is a new COMBINATION of shipped adapter classes:
  an overlay composes only the six `_POLICY_REGISTRIES` axes (schema_sanitizer /
  prompt_policy / response_policy / reasoning_codec / content_policy / cache_policy) plus a
  `params` block (generation kwargs), and needs NO engine code. BUT if no shipped class
  covers the quirk, the fix target is a NEW recovery class (engine code, behind human
  review); still flag it as FORMATTING/fixable even though `reprobe.py` cannot prove it until
  the class ships (precedent: `array_dict_map`, `minimax_m3_tags`).
- GENUINE-MODEL / CONSISTENCY - wrong value, missing step, wrong entity, policy miss,
  flakiness. Not policy-fixable.

Precedence when a trial has MULTIPLE failure reasons: bucket GENUINE-MODEL if it has any
genuine error a formatting/oracle fix would not resolve; bucket FORMATTING only when a
preset fix ALONE would flip the trial to pass; INFRA only per the max_turns carve-out
above. Every failing trial lands in exactly one bucket. This is the prompt's own taxonomy,
NOT the engine's `failure_attribution.py` `failure_class` labels (which are a different,
6-class scheme); do not equate them.

## Aggregate synthesis (compose the per-dimension verdicts into ONE verdict)
1. INFRA is a PRECONDITION in both modes. If `harness-infra` is NOT clean (above
   {{INFRA_THRESHOLD}}), the numbers are void: verdict = RE-RUN, regardless of the rest.
2. Non-model / recoverable pp is owned ONCE. `four-bucket` is the SOLE owner of the split;
   the oracle pp (`task-design-oracle`) and formatting pp (`preset-codec-leak`) are the SAME
   pp already inside four-bucket's buckets. NEVER sum across dimensions.
3. EVAL mode (fidelity): subtract non-model pp (infra + oracle false-fails + formatting/code
   artifacts) from the raw failures. If it is material, the raw number is NOT a faithful
   capability reading -> fix / footnote / regrade / re-run. The true-capability micro pass@1
   = raw + net recoverable pp; compare it (and pass^5) to the GO boundary {{GO_BOUNDARY}}.
   Split the ORACLE share of the recoverable pp three ways, ACCEPTED / NEW-pack / HARNESS (see
   "Accepted circumstances"; FORMATTING and INFRA pp keep their own lines and belong to none of the
   three): all three count toward true capability, but only the last two are work items. The
   aggregate verdict must not list an accepted circumstance as something to fix before publishing,
   and must not bury a live harness bug inside the accepted share. If two dimensions disagree on
   the sub-label for the SAME pp, four-bucket still owns the magnitude, but the LOUDER routing
   governs the reporting (HARNESS > NEW > ACCEPTED) until the human resolves it: never resolve a
   label conflict toward silence.
4. OBSERVE mode (fixability): the verdict is the policy to SET or CREATE for the FORMATTING
   failures (proved by `reprobe.py`) plus the residual GENUINE ceiling; a candidate is
   integrable when the fixable share is closed and the ceiling is acceptable.
5. consistency-vs-capability frames the ceiling story; it does not by itself flip the verdict.

{{GO_BOUNDARY}} and {{INFRA_THRESHOLD}} are HUMAN-OWNED policy inputs (do not invent them);
if unset, report the numbers and defer the call to the human owner.

## Accepted circumstances: what the frozen board has already decided
MODE: EVAL only. The registry for this run is `{{KNOWN_ISSUES}}`. If that reads "n/a" (OBSERVE
mode runs synthetic probes, not the frozen pack), skip this section: every finding is live.
If it is unfilled or unreadable, say so and degrade safely: the ACCEPTED class is simply
unavailable, so report a pack-side finding as NEW. Harness/engine findings do not depend on the
registry and stay loud, never NEW. Never go looking for a registry yourself: it is branch-local,
and a sibling branch's version has a different accepted set, so the wrong file mislabels both ways.

The leaderboard task pack is FROZEN, and for the same comparability reason the engine version is
pinned per board. Dozens of models are already published against both, so editing a task, a
golden or a simulator prompt (or silently adopting an engine fix for one model) would break
comparability and force a board-wide regrade. What the eval owner has already decided to live
with is therefore an ACCEPTED CIRCUMSTANCE, not a work item.

`{{KNOWN_ISSUES}}` is the registry of what has already been triaged. **Read it before you report
anything.** ACCEPTED is decided by the entry, NOT by mere presence in the file: the registry also
lists live bugs that are still open work. Route every non-model finding into one of three classes:

- **ACCEPTED.** Either (i) the entry is task-pack side (a task/golden/simulator/data defect: the
  pack is frozen, so it cannot be fixed for this run), or (ii) the entry carries an explicit
  eval-owner decision to keep the current behaviour (typically "keep running on <pinned engine>"
  for board consistency), whatever side it is on. Still MEASURE it and still attribute its pp: it
  is a real non-model cause, so it belongs in the oracle bucket and in the true-capability number.
  But report it as a one-line *footnote citing the registry entry ID and its measured pp in THIS
  run*. Do NOT propose a task/golden/simulator edit, do NOT propose moving off the pinned engine,
  do NOT recommend a regrade, and do NOT present it as outstanding work. It is the environment
  every model ran in.
- **NEW task-pack defect (no entry covers it).** Report it in full, with the evidence, and propose
  the registry entry it should become. Still do not propose editing the pack; the decision to
  accept, footnote or exclude belongs to the human owner. Match on the entry's own SCOPE (domain +
  field/symptom + mechanism), not on a loose family resemblance; where an entry ENUMERATES task
  IDs, only those IDs are covered, so a new task failing the same way is NEW and merely cites the
  entry as related.
- **HARNESS / ENGINE / ADAPTER with no keep-as-is decision.** Unchanged: report loudly and
  actionably, whether or not the registry already names the bug. An open registry entry is a
  known bug, not an accepted one. These are fixable without touching the pack, they usually
  affect every domain at once, and a fix here is cheap. Never soften one of these into a
  circumstance just because its symptom looks like a task defect.

A METHODOLOGY note (how to compare, how to count cost, a task-set change) is not a finding at all:
it is a rule to FOLLOW while analysing, so obey it and do not report it.

A FIXED marker is VERSION-RELATIVE, so evaluate it against the engine this run actually used,
{{ENGINE_VERSION}}, and never against the marker alone:
- fix IS in this run's version -> history. Cite it only if the symptom recurs anyway, and that
  recurrence is a loud regression finding, not a circumstance.
- fixed only in a LATER version AND the entry carries a keep-as-is decision for the pinned one
  -> ACCEPTED. This combination is common and is precisely what test (ii) exists for.
- fixed only in a later version with NO such decision -> live harness bug, report it loud.
Match every marker CASE-INSENSITIVELY: "DECISION" and "Decision", "FIXED" and "fixed in vX", are
the same marker. A decision reads as a line naming the eval owner with a keep / do-not-change
imperative, in any casing; do not decide an entry's status from its title alone. If containment is
genuinely undeterminable from the version string (a fix that spans repos, e.g. an engine change
plus an adapter bump), decide by SYMPTOM instead: the symptom present under a claimed fix is a
loud regression, absent is nothing to report.

Three things this does NOT license:
- It does not license silence about magnitude. If an accepted circumstance is large enough to
  change the reading of a domain, say so plainly with the number. "Accepted" means "not a
  work item", not "not worth mentioning".
- It does not license assuming uniformity. An accepted circumstance is comparability-safe only
  while it hits every model equally. Compare THIS run's exposure against the board-wide rate the
  entry documents; a large excess is evidence of RANK DISTORTION and must be surfaced as a
  finding even though the underlying defect is accepted. (A single-model run has no cross-model
  data of its own, so the entry's own numbers are the comparator.)
- It does not license silent re-attribution. If routing a finding to ACCEPTED puts pp in a
  different bucket than earlier analyses of the same defect used, say so: the adjusted /
  true-capability number stops being comparable with those analyses, which is a synthesis-level
  caveat, not a detail.

## Interpretive traps (do not fall in)
- Bimodal quirk frequency: a preset-COVERED quirk reads ~0%, an UNcovered one is domain-fatal.
  A ~0% formatting/leak reading is therefore AMBIGUOUS - a shipped preset may be masking a
  dormant quirk. Check whether a non-default preset is active (`task.yaml` effective_preset)
  before concluding "clean-native / no fix". Attribution needs full-eval volume, not a probe.
- pass^5 false friend: see Metrics - never read engine `pass_hat@k` as the board `pass^5`.

## Scope per mode
- EVAL mode uses the scored full-eval layout (`grade.yaml` `binary_pass`, `per_task_metrics.json`).
- OBSERVE mode uses `findings.json`: failures are `capability`/`variants` `per_probe`
  (passed < runs) + `wire.tool_arg_rejections` / `rejected_examples`, NOT `binary_pass`; do
  not report a clean pass from the mere absence of `binary_pass:false`.
- `task-design-oracle` is EVAL-mode only (synthetic observe probes carry their own assert as
  the oracle, so there is no oracle false-failure to hunt). `consistency-passk` in OBSERVE
  mode degrades to a per-PROBE flaky/solid/hard band over the K repeats, not the board pass^k.
- "Accepted circumstances" is EVAL-mode only too: OBSERVE runs synthetic probes, not the frozen
  pack, so there is no registry to consult and every finding is live and actionable, which is
  the point of that mode.

## Efficiency rules (MANDATORY - a prior 12-agent parallel run stalled on I/O)
- Shell-first: Bash with grep/rg/python/jq for ALL bulk work. NEVER loop the Read tool over
  many files. NEVER read `env.yaml`. Prefer `aggregate.json` / `per_task_metrics.json` /
  `findings.json`.
- Read individual `trajectory.yaml` only for a small qualitative sample (default <= ~8 per
  domain; a dimension may set a tighter cap).
- Read-only: modify no eval files. Finish in well under ~25-30 tool calls; report even if
  partial. Return compact markdown, not raw dumps.
