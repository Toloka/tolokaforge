# Multi-service `postgres` reset example

A **runnable** example that exercises the seed-backed reset recipe path
end-to-end through `tolokaforge run`. A real postgres service is labelled
`isolation: reset` and bound to a named SQL seed; the per-trial backend
applies that seed to a fresh stack at the start of every trial.

```
agent → PostgREST (HTTP API) → postgres:16 (real database, reset per trial)
```

## What this example demonstrates

- **A service reset to a named seed per trial.** `app-db` is labelled
  `isolation: reset` with `reset.seed: postgres_baseline` in
  `project.yaml`. The seed (`assets/postgres_baseline.sql`, kind
  `sql_dump`) is registered under `assets.seeds` and applied by the
  per-trial backend right after the compose stack comes up.
- **The reset is load-bearing and observable.** The compose stack's
  `shared/app-db/init.sql` seeds widget 1 with `name = 'factory_default'`.
  The reset seed overwrites it to `name = 'baseline'`. The agent reads the
  widget back over the REST API and writes the value to a file under
  `submissions/`; grading asserts the file reads `baseline`. **If the
  recipe had not fired, the row would still read `factory_default` and
  grading would fail** — that gap is the whole proof.
- **`repeats: 2` fires the recipe twice.** Each trial gets a fresh stack
  and a fresh seed application.

## Why per-trial (not shared)

`app-db` is `reset` and the other three compose services default to
`ephemeral` (fresh per trial). That mix makes the manifest's
`requires_per_trial` true, so backend selection routes the run onto the
per-trial backend — the only backend that applies reset recipes.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_postgres_reset/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_postgres_reset/run_config.yaml
```

First run is slower than the JSON examples — postgres has to initialise
and seed, and PostgREST has to introspect the schema. Compose volumes are
removed at teardown, so every trial starts from a clean `factory_default`
before the reset seed applies.

## Layout

```
examples/native/multi_service_postgres_reset/
├── project.yaml                       # Project spec: assets.seeds + default_environment (app-db: reset)
├── run_config.yaml                    # haiku agent + user, repeats: 2
├── README.md                          # this file
├── assets/
│   └── postgres_baseline.sql          # the reset seed (data-only, overwrites the row with `baseline`)
├── shared/
│   ├── environment.compose.yaml       # 4-service compose (runner + db-service + app-service + app-db)
│   └── app-db/
│       └── init.sql                   # factory schema + `factory_default` row (docker-entrypoint-initdb.d)
└── dataset/tasks/reset_probe/
    ├── task.yaml                       # inherits the project default_environment whole; no environment_manifest
    └── grading.yaml                    # deterministic state check on the written submission
```

## Design notes

- **The seed is data-only (no DDL).** The schema, roles, and table are
  created once by `init.sql` before PostgREST connects, so PostgREST's
  start-up schema cache stays valid. The seed only overwrites a row, which
  keeps `GET /widgets` readable immediately after the reset.
- **The task declares no `environment_manifest`.** It inherits the
  project's `default_environment` whole (including `app-db: reset`). A
  task-level `stack.compose_file` would atomically replace the stack and
  drop the project's `services` map.
- **Deterministic grading, no judge.** The pass/fail signal is a state
  check that the submission contains `baseline`; correctness does not
  depend on an LLM grader.
- **Pinned real images.** `postgrest/postgrest:v12.2.0` and `postgres:16`;
  the engine images are referenced as `tolokaforge-runner:local` /
  `tolokaforge-db-service:local`.

## Related

- [`../multi_service_postgres/README.md`](../multi_service_postgres/README.md) —
  the shared-runtime three-tier postgres example this pack's compose stack
  mirrors.
