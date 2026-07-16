# Reset recipes — seeding a service to a named baseline

A **reset recipe** restores a service to a known baseline at the start of
every trial. A project registers named seed files under `assets.seeds`,
labels a service `isolation: "reset"`, and points it at a seed by name;
the per-trial runtime backend applies that seed right after the service's
container comes up, so the trial body always starts from the same state.

Companion reading: [`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) (the
`RuntimeBackend` seam and per-trial provisioning),
[`PROJECTS.md`](PROJECTS.md) (the Project schema that owns `assets.seeds`
and `default_environment`).

## Seed kinds

A seed's `kind` selects how its bytes are applied. Four kinds ship
(`SeedKind` in [`tolokaforge/core/models.py`](../../tolokaforge/core/models.py)):

| Kind | Applied by | Typical file |
|---|---|---|
| `sql_dump` | `psql` / SQL client executes the dump against the service | `.sql` |
| `filesystem_dir` | Contents copied into the service's workspace | directory |
| `redis_dump` | RDB snapshot loaded into the Redis service | `.rdb` |
| `bare` | Raw file handed to the service verbatim, no interpretation | any |

`sql_dump` and `redis_dump` are inferred from the `.sql` / `.rdb`
extension when a seed is declared as a bare path string; every other case
requires the full `{path, kind, digest}` form.

## Declaring a reset recipe

Three pieces in the project spec wire a recipe (see
[`PROJECTS.md`](PROJECTS.md) for the full schema):

1. **Register the seed** under `assets.seeds`. Each entry carries a
   `path`, a `kind`, and a `sha256:` `digest`. The loader
   (`load_project_config`) verifies the digest against the file bytes and
   fails loud on a mismatch, so a seed swap without re-stamping is caught
   at load time. `tolokaforge assets stamp` fills the digest.
2. **Label the service** `isolation: "reset"` under
   `default_environment.services.<svc>` (or a task's `environment_manifest`).
3. **Bind the seed** with `reset: { seed: <name> }` on that same service.

```yaml
assets:
  seeds:
    postgres_baseline:
      path: "./assets/postgres_baseline.sql"
      kind: "sql_dump"
      digest: "sha256:..."

default_environment:
  services:
    app-db:
      isolation: "reset"
      reset: { seed: "postgres_baseline" }
```

A `reset` service makes `EnvironmentManifest.requires_per_trial` true, so
backend selection routes the run onto `PerTrialRuntimeBackend` (see
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md)). Services left unlabelled
default to `ephemeral` (fresh per trial); the `reset`/`ephemeral` mix
still forces per-trial selection.

## The provision seam

`PerTrialRuntimeBackend.provision` brings up a fresh compose stack per
trial and calls `_apply_reset_recipes`
([`tolokaforge/core/per_trial_runtime.py`](../../tolokaforge/core/per_trial_runtime.py))
immediately after `docker compose up` returns. For every service labelled
`reset`, it resolves `reset.seed` against the backend's seed registry
(populated from `project.assets.seeds`) and calls
`tolokaforge.runtime.reset_recipes.dispatch(seed, service_name, compose)`,
which routes to the handler for the seed's `kind`. A `reset` service with
no `reset.seed`, or a seed name absent from the registry, raises
`ProvisionError` — the recipe never silently no-ops.

Because provision runs once per trial, `repeats: K` applies the seed `K`
times, each onto a freshly-started container.

## Failure modes

Every recipe is fail-loud: a dispatcher that cannot restore its baseline
raises `RuntimeError` naming the service and carrying the failing command's
diagnostic output. What triggers that raise differs by kind:

| Kind | Failure trigger | Surfaced as |
|---|---|---|
| `sql_dump` | `psql` exits non-zero (e.g. a syntax error in the dump; `ON_ERROR_STOP=1`) | `RuntimeError` with the service name, seed path, `rc`, and `psql` stderr |
| `filesystem_dir` | Seed path is not a directory (guard, before any container call); or copy/wipe exits non-zero | `RuntimeError` naming the path |
| `redis_dump` | Corrupt RDB → Redis crash-loops on restart → `PING` never returns `PONG` | ping-stage `RuntimeError` naming the service |
| `bare` | — | Cannot fail from the caller's side: no container action is taken |

At the provision seam, `_apply_reset_recipes` catches that `RuntimeError`
and re-raises it as `ProvisionError(stage="reset_recipe")` whose reason
names the service, the seed, the kind, and the recipe's own error text. The
same stage also covers the pre-dispatch guards — a `reset` service with no
`reset.seed`, or a seed name absent from the registry. Before the
`ProvisionError` propagates, the stack is torn down
(`docker compose down --volumes`), so a failed seed leaves no leaked
containers or networks.

The trial is then marked failed with
`termination_reason=PROVISION_ERROR`: the conductor never runs, the trial
is **non-retryable** (the failure is deterministic), and it counts toward
the run's `failed_attempts`.

A task author sees this in `grade.yaml` as `binary_pass: false` with a
`reasons` string of the form:

```
Provisioning failed at reset_recipe: reset recipe for service 'app-db' (seed 'postgres_baseline', kind 'sql_dump') failed: <recipe error text>
```

carrying the recipe's own diagnostic output at the end.

## Reference examples

Three runnable packs ship as end-to-end references, one per stateful seed kind.

### `sql_dump` — `multi_service_postgres_reset`

[`examples/native/multi_service_postgres_reset`](../../examples/native/multi_service_postgres_reset)
is the `sql_dump` reference. Its compose `init.sql` seeds a widget
row `name = 'factory_default'`; the `postgres_baseline` seed
(`sql_dump`) overwrites it to `name = 'baseline'` at every provision. The
task's agent reads the row back over a PostgREST API and writes it to a
submission file, and deterministic `state_checks` grading asserts the file
reads `baseline`. Had the recipe not fired, the row would still read
`factory_default` and grading would fail — so a green run proves the
`sql_dump` dispatch ran. `tests/integration/test_reset_recipe_end_to_end.py`
drives the pack via `tolokaforge run` at `repeats: 2` and asserts both
trials observe `baseline`.

### `redis_dump` — `multi_service_cache_debug`

[`examples/native/multi_service_cache_debug`](../../examples/native/multi_service_cache_debug)
is the `redis_dump` reference. Its `redis` service is labelled
`isolation: reset` and bound to the `cache_poisoned` seed (`redis_dump`),
an RDB snapshot that pre-loads `order:4021` with a **stale** value
(`status: "processing"`) at every provision, while postgres holds the fresh
truth (`status: "shipped"`). The agent is told the orders API serves stale
data, inspects the app and cache layers over HTTP, and writes a root-cause
note naming the cache-invalidation bug. `tests/integration/test_cache_debug_end_to_end.py`
proves the recipe fired by asserting the captured `services/redis.log`
carries an RDB-load signature — the restart that reloaded `dump.rdb` — so the
`redis_dump` dispatch is witnessed directly from the per-trial backend.

### `filesystem_dir` — `multi_service_endpoint_add`

[`examples/native/multi_service_endpoint_add`](../../examples/native/multi_service_endpoint_add)
is the `filesystem_dir` reference. Its `pristine_source` seed is a **directory
tree** — a small FastAPI orders service (`app.py` plus a `tests/` suite) — bound
to a `testrunner` service labelled `isolation: reset`. The novel piece is the
**shared-volume bridge**: one named volume (`source`) is mounted into both the
`runner` service (as `/work`, the agent's workspace) and the `testrunner` (as
`/workspace`, the recipe's target). At every provision the recipe copies the
pristine tree into that volume, so the source the agent edits over `/work` is the
same source the `testrunner` runs the suite against over `/workspace`. The agent
adds a missing `GET /orders/{id}/summary` endpoint and calls the testrunner's
`POST /run-tests`, whose real `unittest` exit code writes a `PASS`/`FAIL` marker
back into the volume — the decisive `state_checks` grading floor.
`tests/integration/test_endpoint_add_end_to_end.py` witnesses the recipe directly:
after a real `PerTrialRuntimeBackend` provision it reads `runner:/work/app.py` and
confirms it matches the seed (the recipe fired across the bridge), then drives
`POST /run-tests` on the pristine source (marker `FAIL`) and again after writing
the reference endpoint (marker `PASS`), proving the grading floor tracks the real
suite result.
