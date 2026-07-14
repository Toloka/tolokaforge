# Plan: reset-recipe pack via `tolokaforge run`

Issue: #299 (Multi-container runtime v1, keystone of umbrella #304; #300/#302 depend on this)
Branch: feat/reset-recipe-pack

## Context

M3 (#298, promoted via #307) shipped the seed-backed reset recipes
(`sql_dump`, `filesystem_dir`, `redis_dump`, `bare`) and per-recipe
real-container integration tests, but **no task pack exercises the reset
code path via `tolokaforge run` end-to-end.** Discovery confirmed the
exact wiring:

- A service labelled `isolation: "reset"` makes
  `EnvironmentManifest.requires_per_trial` true
  ([`tolokaforge/runner/models.py:1053`](../../tolokaforge/runner/models.py)),
  so backend selection routes the run onto **`PerTrialRuntimeBackend`**
  ([`orchestrator.py:587`](../../tolokaforge/core/orchestrator.py)).
- `PerTrialRuntimeBackend.provision` brings up a **fresh compose stack
  per trial** and calls `_apply_reset_recipes` right after `compose up`
  ([`per_trial_runtime.py:208,359`](../../tolokaforge/core/per_trial_runtime.py)),
  which resolves `services.<svc>.reset.seed` against the project's
  `assets.seeds` registry and dispatches the seed's recipe.
- `trial_executor.py:102` calls `provision(spec)` once per trial, and
  `repeats: K` produces K trials
  ([`orchestrator.py:906`](../../tolokaforge/core/orchestrator.py)), so
  `repeats: 2` ⇒ 2 provisions ⇒ 2 seed applications.
- `tolokaforge run` loads the enclosing `project.yaml` (walking up from
  `--config`) and passes it to the orchestrator
  ([`cli/main.py:224,291`](../../tolokaforge/cli/main.py) via
  `load_effective_run_config`), so `assets.seeds` reaches the backend.

The existing `examples/native/multi_service_postgres` pack uses
`isolation: "shared"` everywhere + `repeats: 1`, so the reset seam never
fires from the CLI. The existing `examples/native/example-microservices-pack`
*declares* `postgres` `reset` but is **not runnable** — it uses a
fictional image (`myrepo/example-backend:v1.4.0`) and expensive
judge-graded tasks; its test
([`tests/integration/test_example_microservices_pack.py`](../../tests/integration/test_example_microservices_pack.py))
is a load/wiring proof only (no Docker, no LLM).

## Goal

Ship a **runnable** pack, `examples/native/multi_service_postgres_reset/`,
that declares `isolation: "reset"` on a real postgres service bound to a
named seed, and prove — with a deterministic grade and a subprocess
`tolokaforge run` integration test at `repeats: 2` — that the reset
recipe fires end-to-end through the CLI.

**The deterministic proof.** Per-trial mode gives every trial a *fresh*
container, so a cross-trial mutation is gone regardless of any recipe —
that alone proves nothing about the recipe. So the pack makes the seed
*load-bearing and observable*: the compose's `init.sql` seeds the row
with `name = 'factory_default'`; the reset seed
(`postgres_baseline.sql`, kind `sql_dump`) overwrites it to
`name = 'baseline'` at provision. The agent reads the row over the REST
API and writes the value to a file; grading asserts the file reads
`baseline`. If `_apply_reset_recipes` → `sql_dump` dispatch → `psql`
had **not** run, the row would still read `factory_default` and grading
would fail. `repeats: 2` exercises the recipe twice.

## Non-goals

- **No new seed kinds** beyond the four M3 shipped.
- **No reset-recipe internals** changes (dispatchers validated at
  integration scope already).
- **No orchestrator behaviour changes** — the wired per-trial seam is
  exercised as-is. The unreachable shared-stack reset seam is a
  Discovered issue, not fixed here.
- **No shared-stack "reset in place between trials"** — that path
  (`SharedStackRuntimeBackend.reset_services_for_next_trial`) is
  unreachable via the CLI (see Discovered issues) and out of scope.

## Stages

### Stage 1: Ship the runnable reset pack + wiring test

- **Contract — new files under `examples/native/multi_service_postgres_reset/`:**
  - `project.yaml` (a Project spec; `find_project_yaml` discovers it by
    walking up from the run config):
    - `assets.seeds.postgres_baseline: {path: ./assets/postgres_baseline.sql, kind: sql_dump, digest: sha256:<stamped>}`.
    - `default_environment.stack: {compose_file: ./shared/environment.compose.yaml, runner_service: runner}`.
    - `default_environment.services.app-db: {isolation: "reset", reset: {seed: postgres_baseline}}`.
      Other compose services (`runner`, `db-service`, `app-service`) are
      left unlabelled → default `ephemeral` (per-trial), matching the
      fresh-per-trial reality; do **not** label them `shared` (a
      `shared` label under a per-trial backend is a semantic wart — see
      the existing microservices pack). The `reset`/`ephemeral` mix
      still makes `requires_per_trial` true, forcing per-trial selection.
    - `task_defaults` (adapter `native`, small `max_turns`, `actors.user`),
      `run_defaults` (compute + `orchestrator` only; storage/observability
      are **intentionally omitted** — see plan note below on the loader
      discriminator-strip bug — and the effective RunConfig picks up the
      identical local-storage / sqlite-queue defaults from `RunConfig`
      itself).
  - `run_config.yaml` at pack root (root file, not a legacy `run_config/`
    dir — sits next to `project.yaml` so discovery resolves it):
    - `models.agent` + `models.user` (cheap model, e.g.
      `openrouter` `anthropic/claude-haiku-4-5`).
    - `orchestrator.repeats: 2`, `workers: 1`, a small `max_turns`.
    - `evaluation.projects: [examples/native/multi_service_postgres_reset/dataset]`
      (canonical field — **not** `task_packs`, to avoid a new
      `DeprecationWarning`), `tasks_glob: tasks/reset_probe/task.yaml`,
      `output_dir: results/multi_service_postgres_reset_example`.
  - `assets/postgres_baseline.sql` — the reset seed. Data-only, no DDL,
    idempotent: `INSERT INTO api.widgets (id, name) VALUES (1, 'baseline') ON CONFLICT (id) DO UPDATE SET name = 'baseline';`
    (data-only keeps PostgREST's schema cache valid — see Risks).
  - `shared/environment.compose.yaml` — mirror the runnable
    `multi_service_postgres` compose: `runner` (`tolokaforge-runner:local`),
    `db-service` (`tolokaforge-db-service:local`), `app-service`
    (`postgrest/postgrest:v12.2.0`, `PGRST_DB_SCHEMAS=api`), `app-db`
    (`postgres:16`, bind-mounts `./app-db/init.sql`). Real images only.
  - `shared/app-db/init.sql` — `CREATE SCHEMA api;` roles
    (`web_anon` NOLOGIN, granted to `authenticator`), `CREATE TABLE
    api.widgets (id int PRIMARY KEY, name text NOT NULL);`, `GRANT
    SELECT ON api.widgets TO web_anon;`, and `INSERT ... (1,
    'factory_default')`. Must sit under the compose file's directory so
    `copy_compose_context` includes it in the per-trial materialisation.
  - `dataset/tasks/reset_probe/task.yaml` — declares **no**
    `environment_manifest` (inherits the project `default_environment`
    whole, incl. `app-db: reset`; declaring a task-level
    `stack.compose_file` would atomically replace the stack and drop the
    project's `services`). `initial_user_message` is **goal-oriented, not
    a walkthrough** (AGENTS.md Task Design Quality Bar item 2; this ships
    under `examples/native/` so users copy it): describe the objective and
    the environment, and let the agent choose its tools, query, and output
    filename — e.g. "A support REST API at `http://app-service:3000`
    serves a `widgets` resource (fields: `id`, `name`); the on-call needs
    the current `name` of widget 1 on record — read it and write it to a
    file under `submissions/`." Do **not** dictate the exact URL/query,
    the `python3 -c urllib` technique, or the exact filename. Determinism
    is preserved by grading's `submissions/*` glob (below), not by
    scripting the prompt. A brief `policies.guidance` note that the runner
    reaches `app-service` by service name (bash allowlist permits python,
    not curl/wget) is fine — that is environment fact, not a walkthrough.
    `tools.agent.enabled: [bash, write_file]`. User simulator cooperative
    (`###STOP###`).
  - `dataset/tasks/reset_probe/grading.yaml` — **deterministic, no
    `llm_judge`**: `state_checks.jsonpaths` on
    `/env/fs/agent-visible/submissions/*` `contains_ci: "baseline"`;
    `transcript_rules.required_actions` for `bash` + `write_file`.
  - `README.md` — what the pack demonstrates and how the `factory_default`
    → `baseline` overwrite proves the recipe fired.
- **Behaviour to lock (canonical):** a wiring test
  `tests/canonical/test_reset_recipe_pack_wiring.py` (no Docker, no keys —
  pure load/resolve/select, correct tier per AGENTS.md). Asserts:
  `load_project_config` verifies the `postgres_baseline` digest;
  `resolve(default_environment, None)` yields a manifest with
  `services["app-db"].isolation == "reset"` and
  `reset.seed == "postgres_baseline"` and `requires_per_trial is True`;
  `Orchestrator._select_backend_from_tasks() == "per_trial"` and the
  constructed `PerTrialRuntimeBackend.seeds` contains `postgres_baseline`.
  (Mirror `test_example_microservices_pack.py`, but marked `canonical`.)
- **Compatibility:** internal only — new example files + a new test. No
  compatibility surface touched (uses only the already-shipped Project
  schema, `assets.seeds`, `ServiceIsolation`, and the `evaluation.projects`
  canonical field).
- **Deliverable:** the pack exists, seed digest is stamped, the canonical
  wiring test passes.
- **Validation:**
  - `uv run tolokaforge assets stamp examples/native/multi_service_postgres_reset/project.yaml` (fills the digest).
  - `uv run pytest -m canonical tests/canonical/test_reset_recipe_pack_wiring.py -v`.
  - `uv run tolokaforge validate --tasks "examples/native/multi_service_postgres_reset/dataset/tasks/**/task.yaml"`.
  - `uv run ruff check` / `ruff format --check` on the new test.
  - Reviewer checks: seed is data-only (no DDL); task declares no
    `environment_manifest`; grading has no `llm_judge`;
    `evaluation.projects` (not `task_packs`); real images only.
- **Doc updates:** `examples/native/multi_service_postgres_reset/README.md`
  (new).

### Stage 2: End-to-end integration test + RESET_RECIPES.md

- **Contract — `tests/integration/test_reset_recipe_end_to_end.py`:**
  Marked `pytest.mark.integration` + `pytest.mark.docker` +
  `pytest.mark.requires_api`. Use the **existing shared auto-skips**, not
  a bespoke key check: `@pytest.mark.requires_api` skips when no
  `ANTHROPIC`/`OPENAI`/`OPENROUTER` key is set (conftest.py:45-53; marker
  registered pyproject.toml:216), and the established Docker-availability
  skip (`@pytest.mark.docker` / the `skip_if_no_docker_runner` fixture)
  covers a missing daemon. (No secret-rule risk — the static-grep test
  scans only `tolokaforge/`, not `tests/`.) Runs
  `tolokaforge run --config examples/native/multi_service_postgres_reset/run_config.yaml`
  via `subprocess` (env loaded the same way `scripts/with_env.sh` does)
  with `repeats: 2`. Asserts:
  - exit code 0;
  - `results/<output_dir>/trials/reset_probe/0/grade.yaml` **and**
    `.../1/grade.yaml` both exist with `binary_pass: true` (the
    `state_checks` component reflects `baseline` observed) — per the
    output layout in
    [`docs/OUTPUT_FORMAT.md`](../OUTPUT_FORMAT.md) §
    `trials/{task_id}/{trial_index}/grade.yaml`;
  - (optional) the run log recorded
    `runtime.backend.selected backend=PerTrialRuntimeBackend`.
- **Behaviour to lock (integration):** the reset recipe fires
  end-to-end via the CLI across two trials; both trials observe the
  seeded `baseline` (i.e. `_apply_reset_recipes` → `sql_dump` dispatch →
  `psql` applied `postgres_baseline.sql` on top of the compose's
  `factory_default`). This is the gap the M3 boundary flagged.
- **Compatibility:** internal only — new test + new doc.
- **Deliverable:** the integration test passes on a real Docker daemon
  with an LLM key; the ship-condition `tolokaforge run` command exits 0.
- **Validation:**
  - `scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_postgres_reset/run_config.yaml` — exits 0, trial 2 grade reads baseline.
  - `scripts/with_env.sh uv run pytest -m integration tests/integration/test_reset_recipe_end_to_end.py -v` — passes.
  - No new `DeprecationWarning` beyond the M3 baseline (uses
    `evaluation.projects`, not `task_packs`).
- **Doc updates:**
  - `docs/architecture/RESET_RECIPES.md` (new) — concise **current-state**
    description of the wired reset path: the four seed kinds; how a pack
    declares `assets.seeds` + `services.<svc>.isolation: "reset"` +
    `reset.seed`; the per-trial provision seam (`_apply_reset_recipes`);
    and `multi_service_postgres_reset` as the reference example. Written
    as if the current state is the only state (no migration history).
    Note that #300 will extend it with failure modes.
  - `docs/architecture/README.md` — add the new doc to the index.

## Discovered issues

- **Fix in this PR (cheap, in the neighbourhood):**
  - `tests/integration/test_example_microservices_pack.py` is marked
    `integration` but needs neither Docker nor an LLM key (pure
    load/resolve/select) — it should be `canonical`. Correcting it while
    adding the sibling `canonical` wiring test keeps the tier discipline
    consistent. **Main decision (2026-07-14): skip this in-PR to keep #299
    tightly scoped; file as a separate follow-up if needed.**

- **Loader discriminator-strip bug (surfaced during Stage 1 implementation).**
  `resolve_effective_run_config_data` in
  [`tolokaforge/core/project_loader.py`](../../tolokaforge/core/project_loader.py)
  dumps `run_defaults` with `exclude_defaults=True`, which strips the
  `type: "local"` discriminator tag from `storage.artifacts` / `storage.logs`,
  making the merged `RunConfig` unloadable with
  `union_tag_not_found`. Any pack declaring `run_defaults.storage` trips
  this — the shipped `examples/native/example-microservices-pack/run_configs/dev.yaml`
  reproduces it, only masked because that pack isn't runnable. Filed as a
  separate GitHub issue (see below); NOT fixed in this PR (touches the
  loader/models, out of #299 scope). This is why Stage 1 ships
  `run_defaults` with only `compute` + `orchestrator` — the pack must be
  runnable end-to-end for Stage 2's proof, and declaring
  `run_defaults.storage` currently makes it unloadable.
- **File as issues (GitHub MCP was unavailable in this session — main
  should file these):**
  1. **Shared-stack reset seam is unreachable via `tolokaforge run`.**
     `SharedStackRuntimeBackend.reset_services_for_next_trial`
     ([`shared_stack_runtime.py:938`](../../tolokaforge/core/shared_stack_runtime.py))
     has **no run-loop caller** — only
     `tests/integration/test_cross_mode_isolation.py` invokes it
     directly. Backend selection routes *every* `reset` service to
     `PerTrialRuntimeBackend` (`requires_per_trial` is true for `reset`),
     and forcing the shared backend with a `reset` service is rejected by
     the isolation-compat gate (`orchestrator.py:826-885`). So the
     shared-stack "reset a service in place between trials" behaviour is
     unreachable from the CLI. Either it is intended for a future
     shared-stack-with-in-place-reset mode (then document it as
     speculative) or it is dead code. This is the real gap behind #299's
     "mutate trial 1 / trial 2 reads baseline" framing. Relates to
     #300 (failure modes) and #303 (startup stress).
  2. **`docs/architecture/ROADMAP.md` 0.11.0 row still reads "Planned"**
     despite M2/M3 having shipped (#298, #307). Doc staleness (AGENTS.md
     Core Rule 8). Recommend folding into the milestone-close task (#7)
     rather than this PR, to keep #299 tightly scoped.

## Risks / open questions

- **The issue's cross-trial mutation framing is reinterpreted.**
  Per-trial fresh containers make a trial-1 mutation vanish in trial 2
  regardless of any recipe, and the shared-reset-in-place seam is
  unreachable (Discovered issue #1). The honest, deterministic proof is
  the `factory_default` → `baseline` overwrite within each trial.
  **Needs user sign-off** that this faithfully satisfies #299's intent
  (exercise the reset code path via `tolokaforge run`).
- **PostgREST schema cache.** PostgREST introspects the schema at
  startup. Keeping the schema/table in `init.sql` (present before
  PostgREST connects) and the reset seed **data-only** (no DDL) avoids a
  stale-cache miss on `GET /widgets`. The implementer must verify the row
  is readable over REST after the seed applies.
- **LLM cost / flakiness in the integration test.** The agent must be a
  real LLM (the `mock` provider emits no tool calls). Mitigated by a
  dead-simple single-GET-plus-write task, a cheap model, a small
  `max_turns`, and deterministic state-check grading (no judge). Cost is
  ~cents × 2 trials. Consider a scripted user simulator to halve the LLM
  cost; keep the agent real.
- **Task discovery.** `evaluation.projects` is set explicitly (pointing
  at `dataset`) rather than relying on the unproven
  empty-projects-→-enclosing-project default; `tasks_glob` selects the
  single task.
- **Compose context copy.** `init.sql` must live under the compose file's
  directory (`shared/app-db/init.sql`) so `copy_compose_context`
  materialises it per trial.
