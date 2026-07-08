# example-microservices-pack

Task pack demonstrating the environment composition model:
content-addressed stack dedup, typed inputs on
`EnvironmentManifest`, the fixed reset-primitive enum, and the
`<pack>/shared/` compose-file convention. Read alongside
[`docs/architecture/ENVIRONMENT_COMPOSITION.md`](../../../docs/architecture/ENVIRONMENT_COMPOSITION.md).

Some of the mechanisms this pack demonstrates land incrementally
through v0.11.0 (see [`ROADMAP.md`](../../../docs/architecture/ROADMAP.md)).
The pack is the canonical worked example authors reference when
they lay out their own multi-task packs.

## Layout

```
example-microservices-pack/
├── shared/
│   └── environment.compose.yaml       # base 4-service stack referenced by 5 tasks
├── run_config.yaml
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # references shared compose — default inputs
    ├── db_query_tuning/               # references shared compose — dedups with above
    ├── postgres_upgrade_test/         # references shared compose — different input value
    ├── schema_isolation_migration/    # ships task-local compose (isolated DB)
    ├── agent_flow_setup/              # semantic-sharing sequence — writes
    └── agent_flow_verify/             # semantic-sharing sequence — reads
```

## The mechanisms, task by task

### Content-addressed dedup

`api_endpoint_add` and `db_query_tuning` both reference the same
shared compose file and neither declares any inputs. Their resolved
compose files are byte-identical; they hash identical. The runtime
materialises **one** stack for both tasks and tears it down when
the last trial of the last task finishes.

Read each task's `task.yaml` side-by-side: the
`environment_manifest.compose_file` path is the same relative path
(`../../shared/environment.compose.yaml`) and neither declares
`environment_manifest.inputs`. Sharing is invisible on the task
side; the runtime handles it.

### Typed inputs

`postgres_upgrade_test` references the same shared compose file
but declares
`environment_manifest.inputs.postgres_version: "17"`. The compose
file's `${postgres_version:-16}` binds to `"17"`; the resolved
compose differs from the two above; the hash differs; this task
gets its own stack. Three tasks, one compose file, two distinct
stacks. Adding a fourth task with `postgres_version: "17"` would
automatically dedup with `postgres_upgrade_test`.

### Fixed reset-primitive enum on services

`shared/environment.compose.yaml` annotates the `postgres` service
with:

```yaml
labels:
  tolokaforge.isolation: "reset"
  tolokaforge.reset_primitive: "postgres_template_db"
```

Between trials the runtime invokes the primitive:
`CREATE DATABASE new TEMPLATE base` semantics — ~200ms to fresh
state without recreating the postgres container. The enum lives in
the engine schema; extending it is a schema change to tolokaforge
itself.

For services where trial state doesn't matter, the compose file
sets `tolokaforge.isolation: "shared"` (no reset between trials).
For services that must not persist state, `"ephemeral"` recreates
the container between trials.

### Convention: shared compose files under `<pack>/shared/`

The pack demonstrates the convention: shared compose in `shared/`,
tasks point at it via `../../shared/environment.compose.yaml`. No
`project.yaml` at the pack root, no overlay layer — the loader's
existing path-resolution handles the reference.

The escape hatch is not a special case. `schema_isolation_migration`
points at a task-local compose file (`./environment.compose.yaml`)
because its migration workload needs an isolated postgres. Because
its resolved compose is different, it hashes to a distinct stack —
same mechanism, different result.

### Semantic sharing (no new field required)

`agent_flow_setup` writes rows to postgres; `agent_flow_verify` is
graded on reading those rows. Both tasks reference the shared
compose file with default inputs — their hashes match — and the
compose file declares `postgres.isolation: "shared"`, so state
persists across trials of both tasks. Task A writes; task B reads.
Sharing is a consequence of identical environment declarations, not
a separate declared property.

**Caveat.** This only works when both tasks run in the same
`tolokaforge run` invocation and the postgres service is declared
`shared`. If it were `reset`, the reset primitive would fire between
every trial, wiping A's writes before B reads them. The task
author's choice of isolation mode encodes the sharing semantics.

## The resolved hash table

Approximate — the actual hash function canonicalises YAML before
hashing.

| Task | Shared compose | Task-local compose | `postgres_version` | Resulting stack |
|---|---|---|---|---|
| `api_endpoint_add` | ✓ | — | `16` (default) | **A** |
| `db_query_tuning` | ✓ | — | `16` (default) | **A** (dedup) |
| `postgres_upgrade_test` | ✓ | — | `17` (override) | **B** |
| `schema_isolation_migration` | — | ✓ | `16` (task-local default) | **C** |
| `agent_flow_setup` | ✓ | — | `16` (default) | **A** (dedup) |
| `agent_flow_verify` | ✓ | — | `16` (default) | **A** (dedup) |

Six tasks → three stacks. Four tasks share stack **A**, one task
each has stack **B** and stack **C**.

## Cross-references

- Architecture doc: [`../../../docs/architecture/ENVIRONMENT_COMPOSITION.md`](../../../docs/architecture/ENVIRONMENT_COMPOSITION.md)
- Runtime backends architecture: [`../../../docs/architecture/RUNTIME_BACKENDS.md`](../../../docs/architecture/RUNTIME_BACKENDS.md)
- Existing multi-service examples for comparison:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this pack extends
  - [`../native_shared_domain/`](../native_shared_domain/) — the existing per-category shared-config precedent
