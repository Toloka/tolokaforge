# Plan: ADR-0016 isolation vocabulary → per-service model

Issue: #370
Branch: docs/issue-370-adr-0016-vocabulary

## Context

`docs/architecture/adr/0016-runtime-backend-comparison.md` still describes isolation
using the pre-amendment whole-manifest `environment_manifest.isolation: per_trial | shared_ok`
field. That field was superseded by the ADR-0018 amendment (2026-07-14): isolation is now
declared **per compose service** as `services.<name>.isolation: shared | reset | ephemeral`,
and backend selection is **task-driven** (any `reset`/`ephemeral` service routes the run to
`PerTrialRuntimeBackend`; `orchestrator.runtime` survives only as a deprecated override).
`RUNTIME_BACKENDS.md` § "Isolation enforcement" (updated in #360) already speaks the current
vocabulary; ADR-0016 must match it.

The issue frames this as a single guardrail-paragraph fix in ADR-0016 (~line 91). Discovery
found the superseded field vocabulary lives in **three files**: ADR-0016 (multiple sites),
the user-facing guide `docs/guides/isolated_trials.md` (including an active instruction telling
operators to write a value that no longer exists), and ADR-0009 (the manifest ADR whose
isolation field ADR-0018 superseded). Leaving any of those live violates AGENTS.md Rule 8.

## Goal

The engine's current-state docs read as if per-service isolation + task-driven backend
selection is the only model that ever existed. Every occurrence of the whole-manifest
`environment_manifest.isolation` field (and its `per_trial` / `shared_ok` values) in the two
current-state docs (ADR-0016, `isolated_trials.md`) is replaced with per-service
`services.<name>.isolation: shared | reset | ephemeral` vocabulary and the task-driven
selection framing, mirroring `RUNTIME_BACKENDS.md` § "Isolation enforcement". ADR-0009 —
a historical record — gets a scoped `Superseded by` header pointer to ADR-0018 for the
isolation surface, its body left intact. The lifecycle-axis `shared` / `per_trial` naming
(backend/mode names + the `--runtime` CLI flag / `orchestrator.runtime` deprecated override —
all still current) is left untouched everywhere.

## Non-goals

- No change to ADR-0016's grading-equivalence section, the A/B table / numbers on
  `coding_public_example_01`, the resource-profile table, observability parity, or
  consequences — those are results/mechanics and stay verbatim.
- No rewrite of ADR-0009's body. Per ADR-0018 (lines ~79–81) only ADR-0009's *isolation
  surface* moved; the compose-as-source-of-truth contract and `EnvironmentManifest` outer
  contract are still current. ADR-0009 is a historical record — it gets a header pointer, not
  a body edit.
- No edit to ADR-0018's amendment section — it is the current-state authority and legitimately
  names the superseded field to record the amendment.
- No rename of the ADR-0016 title or the lifecycle-axis `shared` / `per_trial` mode names, and
  no change to `--runtime per_trial` / `runtime: per_trial` CLI-flag and config examples in
  `isolated_trials.md` — those are Axis-1 / deprecated-override syntax that survive.
- No code, test, or schema-source edits.

## Stages

### Stage 1: Rewrite isolation-field vocabulary to per-service across ADR-0016 + isolated_trials.md; header-pointer ADR-0009

- **Contract:** Documentation-only. No code, config, CLI, or schema surface. Target vocabulary
  everywhere is exactly what `RUNTIME_BACKENDS.md` §"Isolation enforcement" (lines ~271–296)
  and the ADR-0018 amendment (lines ~22–46) use: `shared | reset | ephemeral`,
  `services.<name>.isolation`, unlabelled-defaults-to-`ephemeral`, task-driven selection, any
  `reset`/`ephemeral` service → `PerTrialRuntimeBackend`, all-`shared` (or no `services:` map)
  → `SharedStackRuntimeBackend`, `orchestrator.runtime` deprecated override, guard
  `_verify_isolation_compatibility` firing only on the override path.

  **File 1 — `docs/architecture/adr/0016-runtime-backend-comparison.md` (7 sites):**
  1. **~line 91 — "Failure-mode differences", `shared`-only bullet.** Old guardrail:
     `environment_manifest.isolation` declaration; shared backend refuses `isolation: per_trial`.
     New: the shared backend runs a task only when every service is `isolation: shared` (the
     task author's acknowledgment of the shared-state responsibility); a task declaring any
     `reset`/`ephemeral` service is auto-routed to `PerTrialRuntimeBackend` by task-driven
     selection; the deprecated `orchestrator.runtime` override is the only way to force a shared
     backend against a per-trial-requiring task set, and `_verify_isolation_compatibility`
     refuses that at startup.
  2. **~line 100 — decision-rubric step 1.** `environment_manifest.isolation: per_trial` →
     "declares any service `isolation: reset` or `ephemeral`" → `PerTrialRuntimeBackend`
     (selected automatically).
  3. **~line 101 — decision-rubric step 2.** `environment_manifest.isolation: shared_ok` →
     "declares every service `isolation: shared`" → `SharedStackRuntimeBackend`.
  4. **~line 102 — decision-rubric step 3.** "task does not declare an `environment_manifest`"
     stays factually correct (no manifest → built-in stack → shared); reword only as needed so
     the rubric reads as one task-driven flow.
  5. **~line 103 — decision-rubric step 4 (🟡).** Reframe "Genuinely stateless workload …
     → `shared`" as the *outcome of the declaration*: every service labelled `isolation: shared`
     → run lands on `SharedStackRuntimeBackend` (once per run); author owns the
     no-cross-trial-contamination invariant.
  6. **~line 104 — decision-rubric step 5 (🟡).** Reframe "Cross-trial isolation matters more
     than cost … → `per_trial`" as: any service labelled `reset`/`ephemeral` → run lands on
     `PerTrialRuntimeBackend`; budget the ~10 s / ~9% premium from the A/B numbers above. After
     steps 4–5 are reframed the whole rubric is task-driven and internally consistent — no
     mixed operator-picks-a-mode vs task-driven framing.
  7. **~line 129 — "Follow-ups", "Task-pack migration to per-trial" bullet.** `task packs that
     declare `isolation: per_trial` semantics` → per-service `isolation: reset` / `ephemeral`
     phrasing.

  **File 2 — `docs/guides/isolated_trials.md` (4 sites):**
  8. **~line 91 — manifest example `isolation: "per_trial"`.** Rewrite to a per-service
     example: `services.<name>.isolation: reset` (or `ephemeral`) under the manifest's
     `services:` map.
  9. **~line 101 — quoted `environment_manifest.isolation: per_trial` RuntimeError.** Replace
     with the error string the current override guard emits. The implementer reads the actual
     message from `_verify_isolation_compatibility` in `tolokaforge/core/orchestrator.py` and
     quotes it verbatim — do **not** invent a plausible-looking error.
  10. **~line 105 — active operator instruction "set `isolation: shared_ok` on the task(s)".**
     This is the worst site (tells operators to write a value that no longer exists). Rewrite to
     the current remedy: label the relevant services `isolation: shared` (or drop the deprecated
     `orchestrator.runtime` override), consistent with whatever the current error message advises.
  11. **~line 199 — reference-schema mention `environment_manifest.isolation`.** Update to
     `services.<name>.isolation` per-service form.
  Lifecycle/CLI sites in this guide (`--runtime per_trial`, `runtime: per_trial`, and prose
  using `per_trial` as the isolation *mode* name — lines ~24, ~70, ~80, ~131, ~178, ~180) stay:
  those are valid mode/CLI vocabulary. If the surrounding guide prose reads incoherently after
  the four field-vocab swaps, note it for the reviewer rather than widening silently.

  **File 3 — `docs/architecture/adr/0009-environment-manifest.md` (header only):**
  12. Change the `- **Superseded by:** —` header line to a scoped pointer:
     `- **Superseded by:** ADR-0018 (isolation surface only — 2026-07-14 amendment)`.
     The body (`TaskIsolation`, `shared_ok`/`per_trial` field values, lines ~95–113) is a
     historical record and stays byte-identical.

- **Behaviour to lock:** No runtime behaviour — docs only. Verification is a **file-scoped**
  grep guard, not a test tier:
  - `rg 'shared_ok' docs/architecture/adr/0016-runtime-backend-comparison.md docs/guides/isolated_trials.md` → no matches.
  - `rg 'environment_manifest\.isolation\b' docs/architecture/adr/0016-runtime-backend-comparison.md docs/guides/isolated_trials.md` → no matches.
  - `rg 'services\.<name>\.isolation|isolation: (shared|reset|ephemeral)'` over the same two files → matches present at the rewritten sites.
  - ADR-0009 is **excluded** from the `shared_ok` guard (its body legitimately retains the
    superseded vocabulary as history); the only ADR-0009 change is the header line.
- **Compatibility:** Internal only. ADRs and guides are not compatibility surfaces — no
  CHANGELOG entry, no migration note (the migration is ADR-0018's amendment, already
  documented). All prose is current-state per AGENTS.md Rule 8: no "previously X, now Y".
- **Deliverable:** ADR-0016 (7 sites rewritten, rest byte-identical), `isolated_trials.md`
  (4 sites rewritten, lifecycle/CLI examples intact), ADR-0009 (one header line changed, body
  intact).
- **Validation:**
  - The three `rg` guards above.
  - Reviewer diff-check: only the named sites changed; ADR-0016's grading-equivalence, A/B
    numbers, resource-profile, observability, and consequences sections untouched; ADR-0016
    title and lifecycle mode names untouched; ADR-0009 body untouched; ADR-0018 untouched.
  - Reviewer confirms the `isolated_trials.md` line-101 error string matches the string
    `_verify_isolation_compatibility` actually emits today.
- **Doc updates:** These three docs *are* the deliverable. Confirm no other current-state doc
  carries the superseded field: `rg 'shared_ok|environment_manifest\.isolation\b' docs/` after
  the edit returns matches only inside ADR-0009's body and ADR-0018's amendment section (both
  legitimate historical/authoritative mentions) — nowhere else.

## Discovered issues

- **Fix in this PR:** The issue scoped one ADR-0016 paragraph (~line 91); the superseded field
  vocabulary is actually across three files — ADR-0016 (7 sites incl. the whole decision
  rubric), `docs/guides/isolated_trials.md` (4 sites, incl. a live "set `isolation: shared_ok`"
  operator instruction), and ADR-0009 (header pointer). All folded into Stage 1: fixing only
  line 91 would leave `shared_ok` live elsewhere — the exact Rule-8 violation #370 targets.
- **Filed as issues:** None.

## Risks / open questions

- **`isolated_trials.md` line-101 error string.** The guide quotes a RuntimeError. The current
  override guard (`_verify_isolation_compatibility`) emits a different message than the
  pre-amendment enforcement did; the implementer must quote the *current* string verbatim from
  `tolokaforge/core/orchestrator.py`, not paraphrase. Flagged so the reviewer checks it.
- **Guide coherence beyond the 4 sites.** `isolated_trials.md` is framed around per-trial
  isolation as an operator choice. The four field-vocab swaps keep it Rule-8-clean, but if the
  reviewer finds the surrounding prose now reads as mixed operator-choice vs task-driven, a
  broader guide refresh is a candidate follow-up — out of scope for this vocab fix unless the
  approval gate widens it.
- No behaviour to reproduce: pure docs vocab change, so no dev-MCP repro or test was run in
  discovery (nothing observable to run).
