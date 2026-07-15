# Plan: Remove the unreachable shared-stack reset seam

Issue: #310
Branch: fix/shared-stack-reset-seam

## Context

`SharedStackRuntimeBackend.reset_services_for_next_trial`
([`tolokaforge/core/shared_stack_runtime.py:959`](../../tolokaforge/core/shared_stack_runtime.py))
dispatches reset recipes against the shared compose stack at a "trial
boundary". It has no run-loop caller — the only invocations are the two
direct calls in `tests/integration/test_cross_mode_isolation.py`.

Reproduced empirically (dev MCP `run_python`, against `feat/project-layer-runnability` HEAD `6f055da`):

- A manifest with any `isolation: "reset"` service resolves
  `EnvironmentManifest.requires_per_trial == True`
  ([`tolokaforge/runner/models.py:1053`](../../tolokaforge/runner/models.py)),
  so task-driven selection (`Orchestrator._select_backend_from_tasks`)
  routes the run onto `PerTrialRuntimeBackend`.
- `SharedStackRuntimeBackend.isolation_mode is IsolationMode.SHARED_STACK`,
  so a forced `orchestrator.runtime: shared` override against a
  reset-requiring task set is refused at startup by
  `_verify_isolation_compatibility`
  ([`orchestrator.py:852`](../../tolokaforge/core/orchestrator.py)).
- The method exists only on `SharedStackRuntimeBackend`
  (`hasattr(PerTrialRuntimeBackend, "reset_services_for_next_trial") is False`)
  and is **not** part of the `RuntimeBackend` Protocol
  (`tolokaforge/core/runtime.py`), which does not include it.

Net: the shared-stack "reset a service in place between trials" behaviour
is unreachable from `tolokaforge run`. It is dead code with a test that
exercises it directly.

## Goal

Delete the unreachable seam so the codebase reflects shipped intent, and
lock its absence with a regression test. Make the shared-stack backend's
capability advertisement honest — after the method is gone, the shared
backend can no longer honour any `reset_recipes:*` capability, so it must
stop advertising them.

## Decision — delete (issue option 2), do not make reachable (option 1)

Reasoning, weighed against the axes in the issue:

1. **Semantic honesty — option 1 has no coherent contract.** "In-place
   reset for one service under a shared stack" means four incompatible
   things across the shipped seed kinds
   ([`RESET_RECIPES.md`](../architecture/RESET_RECIPES.md)):
   - `sql_dump` — works *only if* the dump is idempotent
     (`DROP … IF EXISTS; CREATE; INSERT`); re-executed against the
     live, never-recreated container.
   - `filesystem_dir` — a wipe-then-copy of the seeded subtree only;
     any file the agent wrote outside that subtree in the prior trial
     persists. A partial, leaky reset.
   - `redis_dump` — the recipe restarts the container to load the RDB
     (its failure mode is "Redis crash-loops on restart"), which blurs
     the "shared, long-lived container" semantics it is supposed to run
     under.
   - `bare` — takes no container action at all, so "reset" is a genuine
     no-op.
   A single label whose meaning ranges from "clean restore" to
   "silent no-op" is not a contract worth preserving.
2. **No user demand.** Both shipped reset examples
   (`examples/native/multi_service_postgres_reset`,
   `examples/native/example-microservices-pack`) use `PerTrialRuntimeBackend`
   (Case C). No task pack, example, or roadmap entry
   ([`ROADMAP.md`](../architecture/ROADMAP.md)) asks for shared-stack + reset.
3. **Blast radius favours deletion.** Option 1 is a real feature: a new
   run-loop hook, a relaxed routing signal so reset-only tasks can pick
   shared, and a failure contract for a reset that fails *mid-run on an
   already-shared stack* (a failed reset there contaminates every
   subsequent trial — there is no clean per-trial failure boundary).
   Option 2 removes ~44 lines + one integration test.
4. **YAGNI, and re-adding is cheap.** ADR-0018 states the project's own
   position: "premature abstraction risks locking in the wrong shape … we
   Protocol-ise then — a 1-hour mechanical refactor when the demand is
   real, and zero cost to defer." Preserving a seam whose semantics are
   incoherent is not option value; a real shared-reset use case would want
   a freshly, coherently designed contract, not this half-built method.

## Non-goals

- No new run-loop caller, no relaxed routing, no gate relaxation
  (that is option 1, explicitly rejected above).
- No change to `PerTrialRuntimeBackend` — it legitimately applies reset
  recipes and keeps advertising `reset_recipes:*`.
- No rewrite of historical `docs/plans/*` entries that mention the seam
  (plan files are journals, not current-state docs).
- No change to the `orchestrator.runtime` deprecation surface or the
  isolation-compat gate logic — the gate already rejects `shared` + reset
  correctly (see Stage 1 validation).

## Stages

### Stage 1: Delete the unreachable seam + lock its absence

- **Contract:**
  - `SharedStackRuntimeBackend` no longer defines
    `reset_services_for_next_trial`. The method is not part of the
    `RuntimeBackend` Protocol (`tolokaforge/core/runtime.py`) and
    `PerTrialRuntimeBackend` never had it, so no Protocol-conformance or
    other-backend change follows.
  - `tests/integration/test_cross_mode_isolation.py` is deleted in full
    (it is the method's only caller).
- **Behaviour to lock:** add one method to
  `TestSharedStackRuntimePath` in the existing
  `tests/unit/test_orchestrator_isolation_enforcement.py` (tier: **unit** —
  the sibling gate tests already live there) asserting the one genuinely
  new fact: `not hasattr(SharedStackRuntimeBackend,
  "reset_services_for_next_trial")`. A pure structural assertion, no
  external services. Do **not** add a canonical file and do **not**
  re-assert the gate-rejection behaviour — that is already locked in the
  same file by
  `TestSharedStackRuntimePath::test_reset_service_raises` (`:109`), which
  stays green untouched (it exercises the gate, not the deleted method).
- **Compatibility:** internal only. The deleted method is not on the
  `RuntimeBackend` Protocol, not in the published Python API, and has no
  CLI or config surface. No migration note required.
- **Deliverable:** the method and the integration-test file are gone; the
  new `not hasattr(...)` assertion in
  `test_orchestrator_isolation_enforcement.py::TestSharedStackRuntimePath`
  passes; the existing
  `TestSharedStackRuntimePath::test_reset_service_raises`
  (the unit-tier lock on the gate behaviour) still passes untouched.
- **Validation:**
  - `rg -n "reset_services_for_next_trial"` returns **zero** hits outside
    `docs/plans/` (historical journals).
  - `mcp__dev__run_tests` marker `unit`
    path `tests/unit/test_orchestrator_isolation_enforcement.py` — the new
    absence assertion passes and every existing gate test stays green.
  - Reviewer checks: no `_legacy_*` alias or "removed" comment left behind;
    the deletion is clean.
- **Doc updates:** none required. Verified by grep that no current-state
  doc (`RUNTIME_BACKENDS.md`, `RESET_RECIPES.md`,
  `docs/guides/multi_container_tasks.md`, `PROJECTS.md`, ADR-0018) claims
  shared-stack reset as a real or future capability — they uniformly state
  only `PerTrialRuntimeBackend` applies reset recipes. The only prose
  describing the seam was the method's own docstring, which is deleted with
  it. The implementer must re-run the grep and, if any current-state doc
  mention surfaces, rewrite that section so per-trial-only reset reads as
  the only state (no "previously"/"was").

### Stage 2: Make the shared-stack capability advertisement honest

- **Contract:** `SharedStackRuntimeBackend.advertised_capabilities`
  ([`shared_stack_runtime.py:693`](../../tolokaforge/core/shared_stack_runtime.py))
  drops the four `reset_recipes:*` entries and becomes
  `frozenset({"shared_stack", "network_isolation:no_internet"})`.
  `PerTrialRuntimeBackend.advertised_capabilities` is unchanged (it honours
  reset recipes and keeps advertising them). The
  `CAPABILITY_REGISTRY` vocabulary and the `reset_recipes:*`
  `CapabilitySpec` descriptions in
  [`backend_capabilities.py`](../../tolokaforge/core/backend_capabilities.py)
  are unchanged — the names stay legal; only the shared backend's *own*
  advertisement shrinks to what it can honour.
- **Behaviour to lock:** extend
  `tests/unit/test_backend_capabilities.py::TestBackendAdvertisements`
  (tier: **unit**):
  - `"reset_recipes:sql_dump" not in SharedStackRuntimeBackend.advertised_capabilities`.
  - `"reset_recipes:sql_dump" in PerTrialRuntimeBackend.advertised_capabilities`
    (guard against deleting the wrong one).
- **Compatibility:** `compute.capabilities` admission is a run-config
  compatibility surface (Core Rule 5). Migration note in the CHANGELOG
  `Unreleased` section: shared-stack no longer advertises `reset_recipes:*`
  — they were never honourable on a shared stack (reset tasks route to
  `PerTrialRuntimeBackend`, which still advertises them), so a shared-
  selected run that requested `reset_recipes:*` was admitting a capability
  it could not deliver; it is now refused at run start with the standard
  admission error. No shipped run config or example requests reset
  capabilities (verified: `rg` over `examples/` finds none).
- **Deliverable:** shared backend advertises only what it can honour; the
  unit advertisement test pins the split; CHANGELOG records the admission
  change.
- **Validation:**
  - `mcp__dev__run_tests` marker `unit`
    path `tests/unit/test_backend_capabilities.py`.
  - `rg -n "reset_recipes" examples/` and any run-config globs — confirm no
    shipped config requests a reset capability under a shared-selected run.
  - Reviewer checks the CHANGELOG entry names the admission behaviour change
    and the migration.
- **Doc updates:** CHANGELOG `Unreleased` → add a `Fix` entry (see
  Compatibility above). Re-grep docs for any prose asserting the shared
  backend advertises `reset_recipes` — none found today; if one surfaces,
  rewrite it to current state.

## Discovered issues

- **Fix in this PR:** the shared-stack backend advertises four
  `reset_recipes:*` capabilities it can only ever honour through the method
  being deleted. Leaving them advertised would turn our own deletion into a
  fresh false capability claim (Core Rule 1 / honest-capabilities). Cheap
  and squarely in the neighbourhood → fixed in Stage 2. Flagged here so it
  can be vetoed as scope if the reviewer disagrees.
- **Filed as issues:** None.

## Risks / open questions

- **Is Stage 2 in scope for #310?** The issue text names the method, the
  test, the gate, and the docs, but not the capability advertisement. It is
  a direct, provable consequence of the deletion, so this plan folds it in
  rather than filing a follow-up. If the reviewer prefers a tight
  single-concern PR, Stage 2 can be split to a follow-up issue — the plan is
  written so Stage 1 stands alone.
- **The issue's "tighten the gate error message to no longer mention the
  workaround that never existed" is a no-op.** The isolation-compat gate
  messages (`orchestrator.py:896-916`) do not mention a shared+reset
  workaround; the "label every service `isolation: shared`" advice they give
  is a legitimate route to Case B, not a false claim. No gate-message edit
  is warranted, so none is planned.
