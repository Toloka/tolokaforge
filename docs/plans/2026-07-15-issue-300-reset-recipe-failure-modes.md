# Plan: reset-recipe failure-mode coverage + RESET_RECIPES.md failure section

Issue: #300 (Multi-container runtime v1, sub-issue 2/5; depends on #299 which shipped `docs/architecture/RESET_RECIPES.md` and the runnable `multi_service_postgres_reset` pack)
Branch: test/reset-recipe-failure-modes

## Context

M3 gave each reset recipe a fail-loud contract — every dispatcher raises
`RuntimeError` naming the service and carrying the failing command's
output ([`sql_dump.py:64`](../../tolokaforge/runtime/reset_recipes/sql_dump.py),
[`filesystem_dir.py:43`](../../tolokaforge/runtime/reset_recipes/filesystem_dir.py),
[`redis_dump.py:100`](../../tolokaforge/runtime/reset_recipes/redis_dump.py)).
Discovery traced the full propagation path for a recipe failure and it is
sound but **untested end-to-end** and **mislabelled**:

- `PerTrialRuntimeBackend.provision` runs `_apply_reset_recipes` inside the
  *same* `try` that wraps `compose.start()`
  ([`per_trial_runtime.py:198-215`](../../tolokaforge/core/per_trial_runtime.py)).
  A recipe `RuntimeError` is caught, `cleanup_partial_materialisation`
  runs (`docker compose down --volumes` → containers **and** the project
  network removed), then a `ProvisionError` is re-raised — but with
  `stage="provision"` and reason **`"docker compose up failed: …"`**. For a
  reset-recipe failure the compose stack came up fine; the label is a
  factual lie that would misdirect failure attribution to the compose file
  instead of the seed.
- `ProvisioningTrialExecutor.execute` catches that `ProvisionError` and
  synthesises a failed `TrialResult` (`TrialStatus.ERROR`,
  `TerminationReason.PROVISION_ERROR`, `binary_pass=False`, the reason in
  `Grade.reasons`), and **never runs the conductor**
  ([`trial_executor.py:101-111`](../../tolokaforge/core/trial_executor.py)).
  `PROVISION_ERROR` is classified non-retryable
  ([`orchestrator.py:373`](../../tolokaforge/core/orchestrator.py)) and the
  failed trial is counted by `summarize_failure_attributions`
  (`total_failed_attempts`, [`failure_attribution.py:180`](../../tolokaforge/core/failure_attribution.py)).

**Existing coverage already proves the executor's control flow** (trial
marked failed, conductor not run, reason carried) via
`tests/unit/test_trial_executor.py::TestProvisionErrorBranches` using
`InMemoryRuntimeBackend`, and `provision_failure` attribution via
`tests/unit/test_failure_attribution.py::test_provision_error_classification`.
What is missing: (1) that each recipe actually **raises** on the failure
inputs the issue names; (2) that a real `provision` against a live stack
tears the stack down cleanly on a recipe failure (**no orphan containers /
networks**); (3) a **truthful** failure reason distinguishing a reset-recipe
failure from a compose-up failure.

**Note on the issue body's "hardening in `shared_stack_runtime.py`":** that
pointer is misdirected. Reset recipes reachable via `tolokaforge run` route
exclusively through `PerTrialRuntimeBackend` (a `reset` service makes
`requires_per_trial` true → per-trial backend selection). The shared-stack
reset seam has no run-loop caller and is already tracked as an unreachable-seam
issue (#310). All hardening in this plan targets `per_trial_runtime.py`.

## Goal

Lock the reset-recipe failure contract with tests at the right tier, and
give a reset-recipe failure a **clear, correctly-attributed** reason:

1. Each recipe raises `RuntimeError` on the failure input the issue names,
   carrying the service name + diagnostic output (recipe tier).
2. A real `provision` against a live stack, when the recipe fails, raises a
   `ProvisionError(stage="reset_recipe")` whose reason names the service and
   carries the recipe error, and leaves **no orphan containers or networks**.
3. `docs/architecture/RESET_RECIPES.md` gains a current-state "Failure modes"
   section describing what a task author observes.

## Non-goals

- **No change to recipe happy-path semantics** (issue boundary).
- **No retry logic** — a recipe failure is a hard trial failure; the
  existing non-retryable `PROVISION_ERROR` classification stays as-is.
- **No new full `tolokaforge run` subprocess test.** Forcing a recipe to
  fail needs no LLM (the agent never runs); the provision seam is the
  correct, cheaper level to exercise. The green-path CLI run is already
  covered by `tests/integration/test_reset_recipe_end_to_end.py` (#299).
- **No shared-stack work** — its reset seam is unreachable via the CLI (#310).
- **No new seed kinds.**

## Stages

### Stage 1: Truthful reset-recipe failure attribution (`per_trial_runtime.py`)

- **Contract:**
  - `ProvisionStage` (in [`tolokaforge/core/runtime.py:88`](../../tolokaforge/core/runtime.py))
    gains a third member: `Literal["provision", "await_ready", "reset_recipe"]`.
  - `PerTrialRuntimeBackend.provision` splits the single materialisation
    `try` into two: `compose.start()` failures keep
    `ProvisionError(stage="provision", reason="docker compose up failed: …")`;
    `_apply_reset_recipes` failures surface as
    `ProvisionError(stage="reset_recipe", reason=…)`. Both paths still call
    `cleanup_partial_materialisation` before raising (unchanged teardown).
  - `_apply_reset_recipes` owns the `reset_recipe` stage for **all** its
    failure exits: its existing guards (a `reset` service with no
    `reset.seed`; a seed name absent from the registry) move from
    `stage="provision"` to `stage="reset_recipe"`, and the `dispatch(...)`
    call is wrapped so a recipe `RuntimeError` re-raises as
    `ProvisionError(stage="reset_recipe", reason=f"reset recipe for service {name!r} (seed {seed_name!r}, kind {seed.kind!r}) failed: {exc}")`.
    The reason therefore names the service, the seed, the kind, and the
    recipe's own diagnostic text.
- **Behaviour to lock (canonical):** extend
  `tests/canonical/test_per_trial_runtime_backend.py` (uses `_FakeCompose`,
  no Docker):
  - a `reset`-labelled manifest whose service names a seed **absent** from
    `backend.seeds` → `provision` raises `ProvisionError`, `stage == "reset_recipe"`,
    reason names the missing seed, and no client is cached.
  - a `reset`-labelled service with `reset=None` → `ProvisionError(stage="reset_recipe")`.
  - regression guard: a `compose.start()` failure still yields
    `stage == "provision"` (the existing
    `test_compose_start_failure_raises_provision_error` already asserts this —
    confirm it still passes unchanged).
- **Behaviour to lock (unit):** `tests/unit/test_failure_attribution.py`
  already classifies a `PROVISION_ERROR` trajectory as `provision_failure`;
  add one assertion that such an attribution is counted by
  `summarize_failure_attributions` (`total_failed_attempts == 1`). Extend the
  existing test module; do **not** add a new file.
- **Compatibility:** internal only. `ProvisionError` is an in-process
  exception; `stage` flows into log fields and the `Grade.reasons` string
  (diagnostic surfaces), not into any persisted schema, gRPC message, or
  published API. Extending the `ProvisionStage` Literal and adding a stage
  value is an internal refactor — no migration note.
- **Deliverable:** a reset-recipe failure produces
  `ProvisionError(stage="reset_recipe")` with a reason that does not claim
  "docker compose up failed"; canonical + unit locks pass with no Docker.
- **Validation:**
  - `uv run pytest -m canonical tests/canonical/test_per_trial_runtime_backend.py -v`
  - `uv run pytest -m unit tests/unit/test_failure_attribution.py tests/unit/test_trial_executor.py -v`
  - `rg 'stage="provision"|stage == "provision"' tests/ tolokaforge/` — confirm
    no assertion depended on the reset path using `stage="provision"` (the
    two canonical hits are compose-start / missing-manifest, both unchanged).
  - `uv run ruff check` / `ruff format --check` on touched files.
  - Reviewer checks: no `_legacy_*`/shim; `stage="provision"` retained for
    genuine compose-up failures; reason string no longer mislabels a recipe
    failure.
- **Doc updates:** none (docs land in Stage 3, describing the final state).

### Stage 2: Per-recipe failure-mode + clean-teardown integration tests

- **Contract — new `tests/integration/reset_recipes/test_failure_modes.py`**
  (mirrors the existing per-recipe happy-path files: inline `COMPOSE`,
  `SeedRef` built in-test, `pytestmark = [pytest.mark.integration, pytest.mark.docker]`;
  **no** `requires_api` — no LLM is involved). One class per case:
  - **`TestSqlDumpFailure`** — seed is a `.sql` with a syntax error; boot
    `postgres:16-alpine`, call `RECIPE_REGISTRY["sql_dump"].apply(seed, "postgres", compose)`,
    assert it raises `RuntimeError` whose message names the service
    (`'postgres'`), the seed path, a non-zero `rc`, and the `psql` stderr
    (the `ON_ERROR_STOP=1` non-zero exit propagates).
  - **`TestFilesystemDirFailure`** — `seed.path` points at a **file, not a
    directory**; assert `apply(...)` raises `RuntimeError` from the
    `is_dir()` guard naming the path, and (against a live `alpine` stack, as
    in `test_bare_recipe.py`) that no container mutation occurred. The guard
    precedes any Docker call, so this case is fast.
  - **`TestRedisDumpFailure`** — `dump.rdb` is **corrupt** (non-empty garbage
    bytes; an *empty* file is a valid empty DB and would not fail). Boot
    `redis:7-alpine`, `apply(...)`: copy + `restart` succeed, but Redis
    crash-loops on the bad RDB so `PING` never returns `PONG` → the
    ping-stage `RuntimeError` fires. To avoid the full 30 s production poll,
    monkeypatch `redis_dump.RESTART_PING_MAX_ATTEMPTS` down (e.g. to 3) for
    this test — patching a module constant, not faking behaviour; assert the
    raised message names the service and the ping-stage failure.
  - **`bare`** — N/A (documented no-op; covered as such by
    `test_bare_recipe.py`). Add a one-line comment in the module saying why
    `bare` has no failure case, not a skipped test.
  - **`TestProvisionTearsDownCleanlyOnRecipeFailure`** — the clean-teardown
    proof through the real backend. Build a **minimal inline** compose (one
    `postgres:16-alpine` service) + an `EnvironmentManifest` labelling that
    service `isolation: "reset"` → a broken sql seed, and a
    `PerTrialRuntimeBackend(seeds={...})`. Snapshot `docker ps -aq` and
    `docker network ls -q` before; call `backend.provision(spec)`; assert it
    raises `ProvisionError` with `stage == "reset_recipe"` and a reason
    naming the service + recipe text; then assert the after-set of
    containers **and** networks is a subset of the before-set (no new
    survivors) — i.e. `cleanup_partial_materialisation` removed the stack.
    (No runner/db-service needed: `_apply_reset_recipes` raises before runner
    endpoint resolution, so a single postgres service suffices and no
    project images must be pre-built.)
- **Behaviour to lock (integration):** each recipe raises on its named
  failure input carrying service + diagnostic; a real recipe failure inside
  `provision` yields `stage="reset_recipe"` and a Docker-clean teardown.
- **Compatibility:** internal only — new test file, no production change.
- **Deliverable:** all failure-mode tests pass on a real Docker daemon;
  `docker ps -a` / `docker network ls` show no survivors after the
  teardown test.
- **Validation:**
  - `scripts/with_env.sh uv run pytest -m "integration and docker" tests/integration/reset_recipes/test_failure_modes.py -v`
  - Manual `docker ps -a` / `docker network ls` after the run — clean.
  - `uv run ruff check` / `ruff format --check` on the new file.
  - Reviewer checks: correct markers (no `requires_api`); the teardown test
    asserts subset semantics on both containers and networks; redis uses
    corrupt (not empty) bytes; `bare` documented, not skipped.
- **Doc updates:** none (Stage 3).

### Stage 3: RESET_RECIPES.md — "Failure modes" section

- **Contract — `docs/architecture/RESET_RECIPES.md`:**
  - **Replace** the forward-pointer note (current lines ~81-83, "Failure-mode
    behaviour … out of scope … tracked in #300") with a real
    **"Failure modes"** section. Written current-state (AGENTS.md Rule 8):
    no "previously out of scope / now added", no `#300` pointer — the section
    reads as the only state.
  - Content:
    - Per-recipe failure surface: `sql_dump` — `psql` non-zero (e.g. bad SQL)
      → `RuntimeError`; `filesystem_dir` — seed path not a directory → guard
      `RuntimeError` (plus copy/wipe non-zero); `redis_dump` — corrupt RDB →
      restart crash-loop → ping-stage `RuntimeError`; `bare` — cannot fail
      from the caller's side (no container action).
    - The provision-seam response: the recipe `RuntimeError` becomes a
      `ProvisionError(stage="reset_recipe")`; the stack is torn down
      (`docker compose down --volumes` — no leaked containers or networks);
      the trial is marked failed with `termination_reason=PROVISION_ERROR`,
      is **non-retryable** (deterministic), and counts toward the run's
      `failed_attempts`.
    - What a task author sees: `grade.yaml` `binary_pass: false` with
      `reasons` = "Provisioning failed at reset_recipe: reset recipe for
      service … failed: …" carrying the recipe's own error text.
- **Compatibility:** documentation only. `RESET_RECIPES.md` is a
  source-of-truth architecture doc — this is an additive current-state
  section, no migration history.
- **Deliverable:** the doc's failure section matches the tested behaviour;
  the `#300` forward-pointer is gone.
- **Validation:**
  - `rg -n '#300|out of scope|will extend' docs/architecture/RESET_RECIPES.md`
    → no stale forward-pointer remains.
  - Reviewer reads the section against Stage 1/2 behaviour for accuracy;
    confirms current-state voice (no migration narration).
  - `docs/architecture/README.md` already links `RESET_RECIPES.md`
    (cross-cutting concepts row) — no index change needed.
- **Doc updates:** `docs/architecture/RESET_RECIPES.md`.

## Discovered issues

- **Fix in this PR (cheap, in the neighbourhood):** the misleading
  `"docker compose up failed: …"` reason on a reset-recipe failure — fixed by
  Stage 1's `stage="reset_recipe"` split. This is the "small orchestrator
  hardening" the issue body anticipated, re-targeted from
  `shared_stack_runtime.py` to `per_trial_runtime.py` per grounding.
- **Filed as issues:** None. The shared-stack reset seam unreachability is
  already tracked as #310; the redis 30 s ping-poll on a crash-looped
  container is by-design (surfaces the failure) and is handled in-test by
  patching the constant — not worth a follow-up.

## Risks / open questions

- **Stage 1 is a production change in a `test(...)`-titled issue.** It is
  small, internal-only, and severable: if a pure test+doc PR is preferred,
  drop Stage 1 and have Stage 2's teardown test assert the current
  `stage="provision"` / "docker compose up failed" reason instead.
  **Recommendation: keep Stage 1** — the mislabel directly undercuts the
  ship condition's "clear reason", and `_synthesize_provision_failure_result`
  is explicitly built to let analytics distinguish substrate-failure kinds.
  Flagging for user/critic sign-off.
- **`redis_dump` failure timing.** Corrupt RDB → crash-loop → the recipe
  waits `RESTART_PING_MAX_ATTEMPTS` seconds before raising. Mitigated by
  monkeypatching the constant down in the test; without that the test would
  add ~30 s.
- **`filesystem_dir` guard is Docker-independent.** The not-a-directory case
  short-circuits before any Docker call, so it technically needs no daemon;
  it lives in the integration module for cohesion with the other recipe
  cases and is asserted against a live stack to also prove no container
  mutation. Acceptable; noted so the reviewer isn't surprised the assertion
  would pass without Docker.
- **Shared CI box container/network snapshot.** The teardown test asserts the
  after-set is a **subset** of the before-set — that catches any leak (a new
  survivor would fail `after ⊆ before`) while tolerating unrelated
  pre-existing containers/networks. Integration tests run serially in CI
  (`.github/workflows/ci.yml` — no `-n`/xdist), so there's no concurrent-start
  vector. The strongest form would filter by the trial's
  `com.docker.compose.project` label, but the auto-generated project name is
  not exposed on a failed provision; global-diff is the pragmatic choice.
