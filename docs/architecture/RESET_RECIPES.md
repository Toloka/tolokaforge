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

Failure-mode behaviour (partial application, retries, per-service log
capture on a failed seed) is out of scope for this document and tracked
in #300.

## Reference example

[`examples/native/multi_service_postgres_reset`](../../examples/native/multi_service_postgres_reset)
is the runnable end-to-end example. Its compose `init.sql` seeds a widget
row `name = 'factory_default'`; the `postgres_baseline` seed
(`sql_dump`) overwrites it to `name = 'baseline'` at every provision. The
task's agent reads the row back over a PostgREST API and writes it to a
submission file, and deterministic `state_checks` grading asserts the file
reads `baseline`. Had the recipe not fired, the row would still read
`factory_default` and grading would fail — so a green run proves the
`sql_dump` dispatch ran. `tests/integration/test_reset_recipe_end_to_end.py`
drives the pack via `tolokaforge run` at `repeats: 2` and asserts both
trials observe `baseline`.
