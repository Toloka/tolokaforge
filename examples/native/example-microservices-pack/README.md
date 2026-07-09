# example-microservices-pack

Task pack demonstrating the **Project** as the top-level abstraction.
`project.yaml` at the pack root holds shared bases for both tasks
(`task_defaults`) and runs (`run_defaults`); per-item deltas live
in `tasks/<name>/task.yaml` and `run_configs/<name>.yaml`. Seven
tasks show the full range of inheritance and override patterns.
Read alongside
[`docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md).

## Layout

```
example-microservices-pack/
├── project.yaml                       # identity + task_defaults + run_defaults
├── run_configs/
│   └── dev.yaml                       # per-invocation delta on run_defaults
├── shared/
│   ├── environment.compose.yaml       # base 4-service stack referenced by default_environment
│   └── system_prompt.md               # project-level default system prompt
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # full inheritance from project
    ├── db_query_tuning/               # full inheritance from project (dedups with above)
    ├── postgres_upgrade_test/         # partial env override — inputs.postgres_version
    ├── schema_isolation_migration/    # full env override — task-local compose_file
    ├── agent_flow_setup/              # full inheritance — sequence part 1
    ├── agent_flow_verify/             # full inheritance — sequence part 2
    └── long_debugging_session/        # non-env override — max_turns: 60
```

## The base + delta model

`project.yaml` holds two labelled bases:

- **`task_defaults`** — the base for every task. Applies to every
  file under `tasks/**`; each `task.yaml` deep-merges its deltas
  on top.
- **`run_defaults`** — the base for every run config. Applies to
  every file under `run_configs/`; each `run_configs/<name>.yaml`
  deep-merges its deltas on top.

Plus one supporting section:

- **`default_environment`** — the environment every task inherits;
  a task's own `environment_manifest` deep-merges on top.

Per-item files (`task.yaml`, `run_configs/*.yaml`) declare only
what's different. Loader deep-merges base + delta on both layers.

## The tasks, by pattern

### Full inheritance from the project

`api_endpoint_add`, `db_query_tuning`, `agent_flow_setup`,
`agent_flow_verify` — `task.yaml` declares only `task_id` and
`description`. Every other field (adapter_type, max_turns,
system_prompt, user_simulator, environment, models, compute)
inherits from the project. All four resolve to byte-identical
manifests and share **stack A**.

### Partial environment override

[`postgres_upgrade_test/task.yaml`](tasks/postgres_upgrade_test/task.yaml)
overrides one input:

```yaml
environment_manifest:
  inputs:
    postgres_version: "17"
```

Everything else in the environment inherits from the project.
Resolved manifest has a distinct hash → own stack (**stack B**).

### Full environment override

[`schema_isolation_migration/task.yaml`](tasks/schema_isolation_migration/task.yaml)
overrides `compose_file`:

```yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
```

The task's own compose file replaces the project default entirely.
Runs on **stack C**.

### Non-environment task-level override

[`long_debugging_session/task.yaml`](tasks/long_debugging_session/task.yaml)
overrides `max_turns`:

```yaml
task_id: "long_debugging_session"
description: "..."
max_turns: 60
```

The environment inherits from the project (shared stack A); only
`max_turns` differs. The `ToolCallingLoop` reads the resolved
task-effective `max_turns` — 60 instead of 20 — for this task.
Everything else inherits.

## Resolved-config table

Which scope contributes each field for each task.

| Task | `compose_file` | `postgres_version` | `max_turns` | `system_prompt` |
|---|---|---|---|---|
| `api_endpoint_add` | Project | Project (16) | Project (20) | Project |
| `db_query_tuning` | Project | Project (16) | Project (20) | Project |
| `postgres_upgrade_test` | Project | **Task (17)** | Project (20) | Project |
| `schema_isolation_migration` | **Task** | Task (task-local default) | Project (20) | Project |
| `agent_flow_setup` | Project | Project (16) | Project (20) | Project |
| `agent_flow_verify` | Project | Project (16) | Project (20) | Project |
| `long_debugging_session` | Project | Project (16) | **Task (60)** | Project |

## Runtime stacks by hash

Approximate — the actual hash function canonicalises YAML before
hashing.

| Task | Inheritance | `postgres_version` | Resulting stack |
|---|---|---|---|
| `api_endpoint_add` | full inheritance | 16 | **A** |
| `db_query_tuning` | full inheritance | 16 | **A** (shared) |
| `postgres_upgrade_test` | partial env override | 17 | **B** |
| `schema_isolation_migration` | full env override | 16 (task-local) | **C** |
| `agent_flow_setup` | full inheritance | 16 | **A** (shared) |
| `agent_flow_verify` | full inheritance | 16 | **A** (shared) |
| `long_debugging_session` | non-env override (max_turns) | 16 | **A** (shared) |

Seven tasks → three stacks. Five tasks share stack **A**; one each
has stack **B** and stack **C**. Task-level overrides that don't
touch the environment (`max_turns`) don't affect the stack — the
runtime materialises fewer stacks than tasks because the hash is
over the environment, not the full task.

## Run-time deltas

The pack's `run_configs/dev.yaml` is slim by design: it declares
only `models` and `evaluation.output_dir`. Everything else
(`compute`, `storage`, `observability`, `orchestrator`) comes from
`project.run_defaults`. Invoke with
`tolokaforge run --config run_configs/dev.yaml`.

To run the same pack under a different configuration — more
workers, a stronger judge model, a different output directory —
add a new file under `run_configs/` (e.g. `run_configs/ci.yaml` or
`run_configs/nightly.yaml`) with just the fields that differ. The
loader deep-merges `project.run_defaults` under it.

## Cross-references

- Architecture doc: [`../../../docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md)
- Runtime backends: [`../../../docs/architecture/RUNTIME_BACKENDS.md`](../../../docs/architecture/RUNTIME_BACKENDS.md)
- Roadmap: [`../../../docs/architecture/ROADMAP.md`](../../../docs/architecture/ROADMAP.md)
- Related multi-service examples:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this pack extends
  - [`../native_shared_domain/`](../native_shared_domain/) — the canonical demo of the `native` adapter's `_shared/domain.yaml` merge pattern (an adapter-specific sharing convention, separate from the Project schema)
