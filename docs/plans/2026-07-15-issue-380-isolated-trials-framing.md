# Plan: isolated_trials.md — reframe opt-in as task-driven selection

Issue: #380
Branch: docs/issue-380-isolated-trials-framing (commit scope `docs(guides):`)

## Context

`docs/guides/isolated_trials.md` §"How to opt in" (lines 61–98) frames
per-trial isolation as an operator opt-in via "three surfaces, in ascending
order of precedence": `orchestrator.runtime` config, `--runtime` CLI, and a
task-level declaration described as a "hard requirement" where "the
orchestrator refuses to start". This contradicts current behaviour. Backend
selection is task-driven: `Orchestrator._select_backend_from_tasks`
(`tolokaforge/core/orchestrator.py:589`) reads
`EnvironmentManifest.requires_per_trial` for every task and auto-routes any
run with a `reset`/`ephemeral` service onto `PerTrialRuntimeBackend`. The
orchestrator does **not** refuse to start on that path — it selects the
satisfying backend. `_verify_isolation_compatibility`
(`orchestrator.py:852`) early-returns when the selected backend is
`PER_TRIAL_STACK` (line 870–871); its `RuntimeError` fires **only** under the
deprecated `orchestrator.runtime` override path, when an operator forces a
shared backend against a per-trial-requiring task set (or asks for an
`ephemeral` service on a shared backend). The vocabulary in this file is
already current (per #370); this is a framing fix, not a vocabulary fix.

## Goal

§"How to opt in" reads as current state: per-service
`services.<name>.isolation` in the task manifest is the **only** selection
mechanism on the normal path; backend selection is task-driven and automatic;
`orchestrator.runtime` / `--runtime` are **deprecated overrides** for edge
cases; and "refuses to start" language is scoped to the override-conflict path
only. The rewrite must not contradict the intro, "When to use it", or "Worked
example" sections — those get a light touch for coherence where their current
verbs imply an operator picks a backend.

## Non-goals

- No vocabulary changes (`shared`/`reset`/`ephemeral` already correct per #370).
- No code changes. `_select_backend_from_tasks` /
  `_verify_isolation_compatibility` are the authority; the doc conforms to
  them, not vice-versa.
- No changes to `RUNTIME_BACKENDS.md` or ADR-0018 — they already carry the
  task-driven framing (RUNTIME_BACKENDS.md §"Isolation enforcement";
  ADR-0018 §"Amendment"). This plan aligns the guide *to* them.
- No new examples, no restructuring of the "Cost" / "Interaction with
  multi-container tasks" / "Further reading" sections.

## Stages

### Stage 1: Reframe isolation selection as task-driven

- **Contract:** Documentation prose only. No API, CLI, config-schema, or
  wire-format change. The `docs/guides/isolated_trials.md` "How isolation is
  decided" section (renamed from "How to opt in") is the observable surface.
  Concrete edits:
  1. **Rename** `## How to opt in` (line 61) → `## How isolation is decided`.
     No inbound anchor links to `#how-to-opt-in` exist anywhere in the repo
     (verified by grep), so the rename is safe.
  2. **Delete** the "Three surfaces, in ascending order of precedence:"
     framing (line 63) and the numbered surface list (config → CLI →
     task-level).
  3. **New lead:** isolation is decided by the *task*, not opted into by the
     operator. Every compose service carries `services.<name>.isolation`
     (`shared` / `reset` / `ephemeral`; a service with no manifest entry
     defaults to `ephemeral`). Backend selection is task-driven — any
     `reset`/`ephemeral` service on any task in the run routes the whole run
     onto per-trial materialisation automatically; a run whose services are
     all `shared` (or tasks with no manifest at all) stays on the shared
     stack. Present the existing `task.yaml` block (current lines 87–94) as
     THE mechanism.
  4. **Demote overrides:** a subsection ("Deprecated overrides") documenting
     `orchestrator.runtime` (config) and `--runtime` (CLI) as overrides that
     force a backend regardless of the task-driven signal, emitting a
     `DeprecationWarning`. Keep the existing config-YAML and CLI examples
     (current lines 68–81) but framed as overrides for edge cases:
     backwards-compatibility, forcing a shared stack while profile-testing,
     or forcing per-trial on a manifest-less pack to observe isolation
     behaviour (which is exactly what the Worked example does).
  5. **Scope the refusal:** the "hard requirement / orchestrator refuses to
     start" language (current lines 96–113) applies **only** to the
     override-conflict path — an operator forcing `shared` against a
     per-trial-requiring task set. State plainly that the normal
     (task-driven) path never refuses: it auto-selects the satisfying
     backend. Keep the quoted `RuntimeError` block **verbatim** — it must
     stay byte-identical to the message in
     `_verify_isolation_compatibility` (`orchestrator.py:897–908`) — but
     re-caption it as the override-conflict error, not the
     task-declaration consequence.
  6. **Intro (lines 3–6):** reframe "how to opt in. It covers the CLI, the
     run config, and the task-level declaration" → language describing how
     isolation is decided: the per-service declaration that drives backend
     selection, the deprecated CLI/config overrides, and a worked example.
  7. **"When to use it" (lines 33–59) — light touch:** the current
     "Choose `per_trial` when…" / "Choose `shared` when…" verbs read as an
     operator picking a backend, which would contradict the rewritten
     section. Change the framing verbs so the decision maps onto the
     per-service label the *task author* declares (needs fresh state →
     declare `reset`/`ephemeral`; tolerates shared state → declare
     `shared`). Preserve every decision bullet unchanged. The "Default is
     `shared`…" cost paragraph (55–59) stays, reframed so "default" means
     "the task-driven selector lands on the shared stack when no service
     requires isolation" rather than an operator default.
  8. **"Worked example" (lines 116–151) — one clause:** the example uses
     `--runtime per_trial` on a manifest-less pack. Add a short framing
     reference tying that usage back to the "Deprecated overrides"
     subsection (forcing per-trial to observe behaviour), so it doesn't
     re-read as the primary opt-in path. No command changes.
  9. **"Interaction with multi-container tasks" (line 174) — one clause:**
     "Isolation and multi-container are orthogonal — **you choose them
     independently**" is the same operator-picks framing and would ship
     contradicting the rewritten section. Reframe the "you choose them
     independently" clause so the operator is not the one choosing
     isolation (isolation follows the task's per-service labels;
     multi-container follows whether the task declares extra services —
     the two are independent *properties of the task*, not operator
     choices). Preserve the 2×2 table (lines 177–182) and every cell
     intact — the table documents legitimate manifest × backend
     combinations, including override-reachable ones.

- **Behaviour to lock:** No behaviour changes, so no new automated test. The
  one invariant is textual fidelity: the `RuntimeError` block quoted in the
  guide must stay faithful to `_verify_isolation_compatibility`
  (`orchestrator.py:897–908`) — the same wording and the same distinctive
  fragments, with the message's f-string placeholders
  (`{type(...).__name__}`, `{sorted(...)!r}`) rendered as an illustrative
  example and hard-wrapped for the guide. The two are not byte-identical (the
  source is an unformatted f-string with substitution points; the doc block
  is rendered + wrapped) and the criterion does not demand it. Validation is a
  manual comparison of the wording plus an `rg` fragment check (below), not a
  new test tier. Adding a unit/canonical test here would only restate the
  implementation — forbidden.

- **Compatibility:** Documentation of a compatibility surface (the CLI
  `--runtime` flag and `orchestrator.runtime` config field both still exist
  and are unchanged). No migration needed — the flag/field behaviour is
  unchanged; only the guide's framing of them changes. No CHANGELOG entry
  required (no user-facing behaviour change). Current-state prose per
  AGENTS.md Rule 8 — the rewrite reads as if task-driven selection is the only
  state; **no** "previously an opt-in, now task-driven" migration language.

- **Deliverable:** `docs/guides/isolated_trials.md` with §"How isolation is
  decided" (renamed), the overrides demoted to a deprecated-override note, the
  refusal scoped to the override-conflict path, and the intro / "When to use
  it" / "Worked example" sections coherent with the rewrite.

- **Validation:** the implementer runs:
  - `rg -n "opt.in|ascending order of precedence|three surfaces" docs/guides/isolated_trials.md`
    → expect zero hits.
  - `rg -n "how-to-opt-in|How to opt in" docs/ README.md` → expect zero hits
    (no dangling anchors).
  - Compare the quoted `RuntimeError` block against `orchestrator.py:897–908`
    → wording + fragments faithful (placeholders rendered as an example,
    wrapped for the guide); not byte-identical.
  - Re-read the intro, "When to use it", "Worked example", and "Interaction
    with multi-container tasks" (line 174) sections → no residual "choose a
    backend / opt-in surface / you choose them independently" framing that
    contradicts the rewrite. The 2×2 table (177–182) stays intact.
  The reviewer checks: framing matches RUNTIME_BACKENDS.md §"Isolation
  enforcement" and ADR-0018 §"Amendment"; no migration-history prose; the
  refusal is scoped correctly; the override note names it deprecated.

- **Doc updates:** `docs/guides/isolated_trials.md` (this is the doc). No
  other doc references the renamed section, so no cross-file edits.

## Discovered issues

- **Fix in this PR:** the light-touch reframes to the intro, "When to use
  it", "Worked example", and "Interaction with multi-container tasks"
  (line 174) sections (Stage 1 items 6–9). They are in the same file and
  would otherwise contradict the rewritten section — fixing them here is
  cheaper than a follow-up and required for internal coherence.
- **Filed as issues:** #387 (filed by main) — the CLI `--runtime` help text
  (`cli/main.py:188–194`) and the `_print_runtime_banner` output still present
  `--runtime` as a normal selector with no deprecation note, so `--help` will
  read as non-deprecated while this guide calls it deprecated. That is a code
  surface, out of scope for this docs-only PR, and is tracked in #387. (This
  is why the plan does not claim the guide is the *only* stale surface —
  RUNTIME_BACKENDS.md and ADR-0018 already carry the correct task-driven
  framing, but the CLI help/banner does not.)

## Risks / open questions

- **Verbatim error block drift.** The guide quotes the
  `_verify_isolation_compatibility` `RuntimeError` verbatim. If that message
  is later edited without updating the guide, the two drift. This risk
  pre-exists this PR (the block is already quoted); the plan does not worsen
  it. Not worth a doctest given no docs-test harness exists in the repo
  (verified: no markdown lint / link checker in Makefile or CI workflows).
- **"When to use it" scope.** The teammate scoped the issue to lines 61–98,
  but the "Choose per_trial / Choose shared" verbs in "When to use it" would
  contradict the rewrite if left. The plan touches them lightly (verbs +
  linkage to the per-service label only, bullets preserved). If the reviewer
  prefers to leave that section untouched, the fallback is to keep its
  wording and add a single bridging sentence pointing to "How isolation is
  decided" — but the light-touch reframe is the cleaner result.
