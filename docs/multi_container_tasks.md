# Multi-container tasks

This guide walks through authoring a project whose tasks ship their own
docker-compose stack — extra services beyond the engine's built-in
`runner` + `db-service`. It's anchored to a working example
([`examples/native/multi_service_postgres_reset/`](../../examples/native/multi_service_postgres_reset/))
that you can `tolokaforge run` unchanged before adapting it.

For the design rationale + case matrix, see
[ADR-0018](../architecture/adr/0018-multi-container-under-shared-runtime.md).
For the full Project model, see
[`docs/architecture/PROJECTS.md`](../architecture/PROJECTS.md); for the
runtime backend lifecycle, see
[`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md).

## When to declare a multi-container task

The engine already ships built-in stacks — `core_stack` (runner +
db-service) and `full_stack` (adds mock-web + rag-service). If your task
only needs those services, don't declare a stack; just point at the
right run config and the engine wires the built-ins for you.

You want a task-declared stack when the task genuinely needs *something
else running alongside* the runner:

- A real database the agent should query (postgres, mysql, redis, ...).
- A real HTTP API the task provides (a REST endpoint, a mock service, a
  proxied third-party API).
- A queue, cache, or worker the agent has to interact with.
- Any topology the engine defaults don't cover.

You hand the engine a `docker-compose.yaml` you write, and the engine
materialises **exactly** those services — no more, no fewer — for the
task.

## Walkthrough — `multi_service_postgres_reset`

The smallest working demonstration of the shipped semantics lives at
[`examples/native/multi_service_postgres_reset/`](../../examples/native/multi_service_postgres_reset/).
It runs a real postgres behind a PostgREST API, reset to a known seed at
the start of every trial. Three files carry the interesting content:
`project.yaml`, `shared/environment.compose.yaml`, and the sibling
`README.md` that describes the scenario.

### The project-YAML declaration

The stack and its per-service treatment are declared on the project under
`default_environment`. Every task inherits this unless it declares its own
`environment_manifest`.

```yaml
# examples/native/multi_service_postgres_reset/project.yaml
assets:
  seeds:
    postgres_baseline:
      path: ./assets/postgres_baseline.sql
      kind: sql_dump
      digest: sha256:...

default_environment:
  stack:
    compose_file: ./shared/environment.compose.yaml
    runner_service: runner
  services:
    app-db:
      isolation: reset
      reset:
        seed: postgres_baseline
    # the other compose services carry no entry → they default to
    # `ephemeral` (fresh per trial); no entry needed.
  network_policy: no_internet
```

The pieces that matter:

- **`stack.compose_file`** — path to the docker-compose YAML, relative to
  the file that declares it. This file is the sole source of truth for
  images, ports, volumes, health probes, and inter-service dependencies.
  The engine reads it verbatim.
- **`stack.runner_service`** — which service in the compose file is the
  tolokaforge runner. The engine talks gRPC to this service to dispatch
  tool calls and grade the trial.
- **`services.<name>.isolation`** — the per-service isolation treatment.
  The compose file carries **zero** isolation semantics; the `services`
  map is the single authority. See [choosing isolation](#choosing-isolation)
  below.
- **`services.<name>.reset.seed`** — for a `reset` service, the named seed
  from `assets.seeds` applied at every provision. See
  [reset recipes](#reset-recipes) below.
- **`network_policy`** — the egress posture for task services. See
  [network policy](#network-policy) below.

A task can declare its own `environment_manifest` with the same shape; it
deep-merges on top of `default_environment` per service. A task that
supplies its own `stack.compose_file` replaces the stack atomically and
drops the project's `services` entries with it.

### The compose file

The compose file lists the services the engine should bring up:

```yaml
# examples/native/multi_service_postgres_reset/shared/environment.compose.yaml
services:
  runner:
    image: tolokaforge-runner:local
    environment:
      DB_SERVICE_URL: "http://db-service:8000"
    ports:
      - "50051"
    depends_on:
      db-service:
        condition: service_healthy
      app-service:
        condition: service_healthy
    healthcheck: ...

  db-service:
    image: tolokaforge-db-service:local
    ports:
      - "8000"
    healthcheck: ...

  app-db:
    image: postgres:16
    healthcheck: ...

  app-service:
    image: postgrest/postgrest:v12.2.0
    depends_on:
      app-db:
        condition: service_healthy
    healthcheck: ...
```

Things worth noting:

- **`tolokaforge-runner:local` and `tolokaforge-db-service:local` are
  aliases** the engine sets up at run start. Task compose files reference
  these stable names instead of the content-hash tags the engine actually
  builds. Details in
  [`RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md).
- **Services reach each other by service name.** All services in a compose
  file join the same auto-generated docker network, so the runner container
  reaches `app-service` as `http://app-service:3000/`. No manual network
  wiring needed.
- **Pinned image tags are enforced.** The validator rejects floating tags
  like `postgres:latest` — pin to a specific version (`postgres:16` above)
  so runs stay reproducible.

The engine validates a few more safety invariants when it loads the stack:
no `network_mode: host`, no `privileged: true`, no `cap_add`, `depends_on`
must resolve, `runner_service` must be declared in the compose file.
Violations fail at load time with a clear error, not at trial start.

## Choosing isolation

Isolation is declared **per service**, under
`services.<name>.isolation`. There are three values:

| `isolation` | Between trials | Use when |
| --- | --- | --- |
| `shared` | The container persists; all trials share it. | The service is long-lived and stateless, or its state is meant to accumulate across trials. Fastest. |
| `reset` | A fresh container per trial, with a named seed applied at each provision. | The agent mutates the service (DB writes, side-effects) and each trial must start from a known baseline. |
| `ephemeral` | A fresh container per trial, no seed. | The agent mutates the service and a clean substrate — not a specific seed — is enough. This is the **default** for any service without an entry. |

Backend selection is **automatic** and driven by the tasks, not by a flag.
The orchestrator reads each task's resolved environment: if **any** service
is `reset` or `ephemeral`, the manifest requires per-trial isolation and
the run routes to `PerTrialRuntimeBackend` (the only backend that tears
down and re-provisions between trials, and the only one that applies reset
recipes). If every service is `shared`, the run uses the shared-stack
backend, which materialises the stack once and shares it across all trials.

Rule of thumb: leave a service unlabelled (defaults to `ephemeral`) unless
you have a reason not to. Declare `shared` only after verifying the service
carries no cross-trial state that could leak into grading; declare `reset`
when a trial needs a specific known baseline it can mutate freely.

## Reset recipes

A `reset` service binds to a named seed via
`services.<name>.reset.seed: <name-from-assets.seeds>`. The seed itself is
declared once under `project.assets.seeds` with a `path`, a `kind`, and a
`digest`. At the start of every trial the per-trial backend brings up a
fresh stack and applies the seed to the named service.

Four seed kinds are supported — `sql_dump`, `filesystem_dir`,
`redis_dump`, and `bare`. For the full authoring reference (how each kind
is applied, extension inference, and failure modes), see
[`docs/architecture/RESET_RECIPES.md`](../architecture/RESET_RECIPES.md).

## Network policy

`network_policy` sets the egress posture for task services. The default is
`no_internet`: task services have no public egress, while the runner keeps
an edge network so LLM-judge grading can still reach model providers.

`full_internet` is the explicit opt-in for a task that legitimately needs
egress (fetching a remote resource, calling a third-party API under test).
`limited_internet` fails loud today — the egress-proxy sidecar it needs is
not yet shipped — so declaring it is a load-time error, not a silent
degrade.

## Adding another service

Take the working `multi_service_postgres_reset` example and extend its
compose file. Suppose you want to add a redis cache the agent mutates:

1. Copy the example into your project.
2. Add a `cache` service to `shared/environment.compose.yaml`:
   ```yaml
   services:
     # ... existing runner, db-service, app-db, app-service ...
     cache:
       image: redis:7.4-alpine     # pinned tag — floating tags rejected
       ports:
         - "6379"
       healthcheck:
         test: ["CMD", "redis-cli", "ping"]
         interval: 2s
         retries: 30
         start_period: 2s
   ```
3. Give it an isolation treatment in `project.yaml`. Leave it out to get
   the `ephemeral` default, or reset it to a seed:
   ```yaml
   default_environment:
     services:
       cache:
         isolation: reset
         reset:
           seed: cache_baseline   # declare under assets.seeds, kind: redis_dump
   ```
4. If the runner needs to reach it, add a `depends_on` and (optionally) an
   env var in the compose file:
   ```yaml
   services:
     runner:
       environment:
         REDIS_URL: "redis://cache:6379"
       depends_on:
         cache:
           condition: service_healthy
   ```
5. Validate and run:
   ```bash
   uv run tolokaforge validate --tasks "my_project/**/task.yaml"
   scripts/with_env.sh uv run tolokaforge run --config my_project/run_config.yaml
   ```

Because `cache` is `reset` (and `app-db` already is), the run requires
per-trial isolation and routes to `PerTrialRuntimeBackend` automatically.

## Further reading

- [`examples/native/multi_service_postgres_reset/README.md`](../../examples/native/multi_service_postgres_reset/README.md)
  — the anchor example this guide walks through (per-service isolation +
  `sql_dump` reset seed)
- [`examples/native/multi_service_slow_start/README.md`](../../examples/native/multi_service_slow_start/README.md)
  — startup-order stress: a slow dependency that the orchestrator waits on
  via healthcheck before the runner fires
- [`examples/native/multi_service/README.md`](../../examples/native/multi_service/README.md)
  — the task-level shared multi-container pattern (nginx catalog)
- [`examples/native/multi_service_postgres/README.md`](../../examples/native/multi_service_postgres/README.md)
  — a realistic three-tier stack (PostgREST + postgres, shared runtime)
- [`examples/native/example-microservices-pack/`](../../examples/native/example-microservices-pack/)
  — the schema reference pack: full inheritance/override matrix across five
  tasks (reference only, see its README before running)
- [`docs/architecture/PROJECTS.md`](../architecture/PROJECTS.md)
  — the full Project model: assets, `default_environment`, per-service
  isolation, and the merge chains
- [`docs/architecture/RESET_RECIPES.md`](../architecture/RESET_RECIPES.md)
  — the four seed kinds and how a reset recipe is applied
- [ADR-0018](../architecture/adr/0018-multi-container-under-shared-runtime.md)
  — case matrix + sequence diagrams for each supported combination
- [`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md)
  — full lifecycle + materialisation deep-dive
