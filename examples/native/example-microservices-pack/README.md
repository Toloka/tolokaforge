# example-microservices-pack

Reference project demonstrating the **Project** as the top-level
abstraction. `project.yaml` at the project root holds shared bases
for both tasks (`task_defaults`) and runs (`run_defaults`);
per-item deltas live in `tasks/<name>/task.yaml` and
`run_configs/<name>.yaml`. Five tasks show the full range of
inheritance and override patterns. Read alongside
[`docs/PROJECTS.md`](../../../docs/PROJECTS.md).

> **Status.** Reference project for the Project schema — full
> inheritance/override matrix across 5 tasks. See
> [`docs/PROJECTS.md`](../../../docs/PROJECTS.md).
> `backend-api` runs [PostgREST](https://postgrest.org), which
> auto-generates a REST API from the postgres schema — the pack ships
> no application code. The compose stack materialises and serves the
> seeded schema; the loader and per-task manifest resolution work
> end-to-end. The tasks use the minimal `task_id` + `description`
> shape PROJECTS.md documents.

## Layout

```
example-microservices-pack/
├── project.yaml                       # identity + assets + task_defaults + run_defaults
├── run_configs/
│   └── dev.yaml                       # per-invocation delta on run_defaults
├── shared/
│   ├── environment.compose.yaml       # base 4-service stack referenced by default_environment
│   ├── seeds/
│   │   └── app_baseline.sql           # named seed: postgres reset baseline
│   └── system_prompt.md               # project-level default system prompt
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # full inheritance from project
    ├── db_query_tuning/               # full inheritance from project
    ├── postgres_upgrade_test/         # partial env override — inputs.postgres_version
    ├── schema_isolation_migration/    # full env override — task-local compose_file
    └── long_debugging_session/        # non-env override — max_turns + grading delta
```

## The base + delta model

`project.yaml` holds two labelled bases:

- **`task_defaults`** — the base for every task. Applies to every
  file under `tasks/**`; each `task.yaml` deep-merges its deltas
  on top.
- **`run_defaults`** — the base for every run config. Applies to
  every file under `run_configs/`; each `run_configs/<name>.yaml`
  deep-merges its deltas on top.

Plus two supporting sections:

- **`default_environment`** — the environment every task inherits;
  a task's own `environment_manifest` deep-merges on top. The
  `stack` sub-object is the substrate slot (compose pointer +
  `inputs` + `runner_service`); a task patch containing the
  `compose_file` key replaces it atomically, while a patch
  touching only `inputs`/`runner_service` deep-merges;
  per-service semantics (isolation, reset recipes) are declared
  outside it under `services:` — the compose file only defines
  what the services *are*.
- **`assets`** — named, typed files tasks and service recipes
  reference by name. This project declares one seed
  (`app_baseline`, kind `sql_dump`), which is both what postgres
  starts from and what the `reset` recipe restores between
  trials.

Per-item files (`task.yaml`, `run_configs/*.yaml`,
`grading.yaml`) declare only what's different. The loader
deep-merges base + delta on both chains.

## Isolation, spelled out

The project declares no `default_environment.isolation`, so the
`per_trial` default applies (safe by default, ADR-0009): any
service without a `services:` entry is **ephemeral** — torn down
and recreated between trials. The entries relax that only where
the project author asserts it's safe:

| Service | Declaration | Between trials |
|---|---|---|
| `postgres` | `isolation: reset`, seed `app_baseline` | restored to the seed baseline |
| `db-service` | `isolation: shared` | persists (stateless) |
| `backend-api` | `isolation: shared` | persists (stateless) |
| `runner` | *(none)* | ephemeral (the default) |

The `schema_isolation_migration` task replaces `compose_file`,
which discards these entries entirely — its task-local stack
falls back to all-ephemeral, exactly right for an irreversible
migration.

## The tasks, by pattern

### Full inheritance from the project

`api_endpoint_add`, `db_query_tuning` — `task.yaml` declares only
`task_id` and `description`; `grading.yaml` declares only the
rubric. Everything else (adapter_type, max_turns, system_prompt,
actors, environment, grading combine) inherits from the project.

### Partial environment override

[`postgres_upgrade_test/task.yaml`](tasks/postgres_upgrade_test/task.yaml)
overrides one input:

```yaml
environment_manifest:
  stack:
    inputs:
      postgres_version: "17"
```

Everything else in the environment inherits from the project.

### Full environment override

[`schema_isolation_migration/task.yaml`](tasks/schema_isolation_migration/task.yaml)
overrides `compose_file`:

```yaml
environment_manifest:
  stack:
    compose_file: "./environment.compose.yaml"
    runner_service: "runner"
```

Replacing `stack.compose_file` replaces the project's whole
`stack` object (clean slate of `inputs`/`runner_service`) and
drops the project's per-service `services` entries with it.

### Non-environment task-level overrides

[`long_debugging_session/task.yaml`](tasks/long_debugging_session/task.yaml)
overrides `max_turns: 60` (the task chain resolves 60 instead of
the project's 20; no run-level cap is set, so 60 stands), and its
`grading.yaml` overrides one combine field
(`pass_threshold: 0.7`) while inheriting the rest from
`task_defaults.grading_defaults`.

## Resolved-config table

Which scope contributes each field for each task.

| Task | `compose_file` | `postgres_version` | `max_turns` | grading `combine` |
|---|---|---|---|---|
| `api_endpoint_add` | Project | Project (16) | Project (20) | Project defaults |
| `db_query_tuning` | Project | Project (16) | Project (20) | Project defaults |
| `postgres_upgrade_test` | Project | **Task (17)** | Project (20) | Project defaults |
| `schema_isolation_migration` | **Task** | — (no input; task-local compose pins `postgres:16`) | Project (20) | Project defaults |
| `long_debugging_session` | Project | Project (16) | **Task (60)** | **Task (threshold 0.7)** |

## Run-time deltas

The project's `run_configs/dev.yaml` is slim by design: it
declares only `models` and `evaluation.output_dir`. Everything
else (`compute`, `storage`, `observability`, `orchestrator`)
comes from `project.run_defaults`. Invoke with
`tolokaforge run --config run_configs/dev.yaml`.

To run the same project under a different configuration — more
workers, a stronger judge model, a different output directory —
add a new file under `run_configs/` (e.g. `run_configs/ci.yaml`
or `run_configs/nightly.yaml`) with just the fields that differ.
The loader deep-merges `project.run_defaults` under it.

## Cross-references

- Architecture doc: [`../../../docs/PROJECTS.md`](../../../docs/PROJECTS.md)
- Runtime backends: [`../../../docs/RUNTIME_BACKENDS.md`](../../../docs/RUNTIME_BACKENDS.md)
- Roadmap: [`../../../docs/ROADMAP.md`](../../../docs/ROADMAP.md)
- Related multi-service examples:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this project extends
  - [`../native_shared_domain/`](../native_shared_domain/) — the canonical demo of the `native` adapter's `_shared/domain.yaml` merge pattern (an adapter-specific sharing convention, separate from the Project schema)
