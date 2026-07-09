# example-microservices-pack

Task pack demonstrating the **Project** as the top-level abstraction
with the **Project → Category → Task** authoring hierarchy on top
of a slim per-invocation `run_config.yaml`. A `project.yaml` at
pack root declares the shared `default_environment`, `task_defaults`,
`models`, `compute`, `storage`, `observability`, and `orchestration`.
A `_shared/domain.yaml` demonstrates the category tier that layers
between Project and Task. Nine tasks show the full range of
inheritance and override patterns. Read alongside
[`docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md).

## Layout

```
example-microservices-pack/
├── project.yaml                       # top-level Project spec (every section)
├── run_config.yaml                    # per-invocation config (slim)
├── shared/
│   ├── environment.compose.yaml       # base 4-service stack referenced by default_environment
│   └── system_prompt.md               # project-level default system prompt
├── _shared/                           # category tier — customer_support domain
│   ├── domain.yaml                    # shared category / tools / user_simulator / system_prompt
│   └── system_prompt.md               # category-level system prompt
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # full inheritance from project
    ├── db_query_tuning/               # full inheritance from project (dedups with above)
    ├── postgres_upgrade_test/         # partial env override — inputs.postgres_version
    ├── schema_isolation_migration/    # full env override — task-local compose_file
    ├── agent_flow_setup/              # full inheritance — sequence part 1
    ├── agent_flow_verify/             # full inheritance — sequence part 2
    ├── long_debugging_session/        # non-env override — max_turns: 60
    ├── support_triage_01/             # category tier — inherits _shared/domain.yaml
    └── support_triage_02/             # category tier + task-level nested override
```

## The three authoring tiers

Reading top-down:

- **Project** (`project.yaml`) — pack-level defaults for every
  section: identity, environment, task defaults, models, compute,
  storage, observability, orchestration.
- **Category** (`_shared/domain.yaml`) — a task under a category
  references the domain file via `domain: ../../_shared/domain.yaml`.
  The loader deep-merges the domain's `category`, `tools`,
  `user_simulator`, and `system_prompt` fields onto the project
  defaults. Task wins over category; category wins over project.
- **Task** (`task.yaml`) — per-task identity + any overrides. A
  task with only `task_id` and `description` inherits everything
  else.

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

Everything else in the environment (compose_file, runner_service,
other inputs, isolation, network_policy) inherits from the project.
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

### Category-tier inheritance

[`support_triage_01/task.yaml`](tasks/support_triage_01/task.yaml)
references the customer_support domain:

```yaml
task_id: "support_triage_01"
description: "..."
domain: "../../_shared/domain.yaml"
```

The loader deep-merges `_shared/domain.yaml`'s fields onto the
project defaults for this task. So `category`, `tools`,
`user_simulator`, and `system_prompt` come from the domain (which
declares them for the customer_support scenario); the environment,
compute, models, etc. still come from the project.

### Category + task-level nested override

[`support_triage_02/task.yaml`](tasks/support_triage_02/task.yaml)
also references the domain, but additionally overrides one nested
field:

```yaml
domain: "../../_shared/domain.yaml"

user_simulator:
  persona: "polite enterprise customer, VP of engineering"
  # mode and backstory inherit from the domain
```

Three-tier merge: project.task_defaults.user_simulator → domain.yaml
user_simulator → task.yaml user_simulator. Task fields win over
domain; domain fields win over project. `persona` from task,
`mode` + `backstory` from domain, `temperature` from project.

## Resolved-config table

Highlights which scope contributes each field for each task.

| Task | `compose_file` | `postgres_version` | `max_turns` | `system_prompt` | `tools` | `user_simulator.persona` |
|---|---|---|---|---|---|---|
| `api_endpoint_add` | Project | Project (16) | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `db_query_tuning` | Project | Project (16) | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `postgres_upgrade_test` | Project | **Task (17)** | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `schema_isolation_migration` | **Task** | Task (task-local default) | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `agent_flow_setup` | Project | Project (16) | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `agent_flow_verify` | Project | Project (16) | Project (20) | Project | Project ({}) | Project ("curious engineer") |
| `long_debugging_session` | Project | Project (16) | **Task (60)** | Project | Project ({}) | Project ("curious engineer") |
| `support_triage_01` | Project | Project (16) | Project (20) | **Category** | **Category** | **Category** ("frustrated support customer") |
| `support_triage_02` | Project | Project (16) | Project (20) | **Category** | **Category** | **Task** ("polite enterprise customer, VP of engineering") |

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
| `support_triage_01` | category-tier inheritance | 16 | **A** (shared) |
| `support_triage_02` | category tier + task override | 16 | **A** (shared) |

Nine tasks → three stacks. Seven tasks share stack **A**; one each
has stack **B** and stack **C**. Task-level overrides that don't
touch the environment (e.g. `max_turns`, `user_simulator.persona`)
don't affect the stack — the runtime materialises fewer stacks than
tasks because the hash is over the environment, not the full task.

## Run-time override

The pack's `run_config.yaml` is slim by design: it declares only
per-invocation fields (`orchestrator.repeats`, `evaluation.task_packs`)
and lets everything else inherit from the project. To run the same
pack under a different configuration — say more workers, a stronger
judge model, a different output directory — copy `run_config.yaml`
to a new file, uncomment the fields you want to override, and pass
it via `--config`.

## Cross-references

- Architecture doc: [`../../../docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md)
- Runtime backends: [`../../../docs/architecture/RUNTIME_BACKENDS.md`](../../../docs/architecture/RUNTIME_BACKENDS.md)
- Roadmap: [`../../../docs/architecture/ROADMAP.md`](../../../docs/architecture/ROADMAP.md)
- Related multi-service examples:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this pack extends
  - [`../native_shared_domain/`](../native_shared_domain/) — the existing per-category shared-config precedent this pack builds on
