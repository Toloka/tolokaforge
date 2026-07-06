# Multi-container tasks

This guide walks through authoring a task that ships its own docker-compose
stack — extra services beyond the engine's built-in `runner` + `db-service`.
It's anchored to a working example
([`examples/native/multi_service/`](../../examples/native/multi_service/))
that you can `tolokaforge run` unchanged before adapting it.

For the design rationale + case matrix, see
[ADR-0018](../architecture/adr/0018-multi-container-under-shared-runtime.md).
For the runtime backend lifecycle, see
[`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md).

## When to declare a multi-container task

The engine already ships built-in stacks — `core_stack` (runner +
db-service) and `full_stack` (adds mock-web + rag-service). If your task
only needs those services, don't declare a manifest; just point at the
right run config and the engine wires the built-ins for you.

You want a task-declared manifest when the task genuinely needs *something
else running alongside* the runner:

- A real database the agent should query (postgres, mysql, redis, ...).
- A real HTTP API the task provides (a REST endpoint, a mock service, a
  proxied third-party API).
- A queue, cache, or worker the agent has to interact with.
- Any topology the engine defaults don't cover.

The manifest hands the engine a `docker-compose.yaml` you write, and the
engine materialises **exactly** those services — no more, no fewer — for
the task.

## Walkthrough — `multi_service_example_01`

The smallest working demonstration lives at
[`examples/native/multi_service/`](../../examples/native/multi_service/).
Three files carry the interesting content: `task.yaml`,
`environment.compose.yaml`, and the sibling `README.md` that describes the
scenario.

### The task-YAML declaration

The task declares its manifest via `environment_manifest`:

```yaml
# examples/native/multi_service/dataset/tasks/multi_service/multi_service_example_01/task.yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  isolation: "shared_ok"
  runner_service: "runner"
```

Three fields matter:

- **`compose_file`** — path to the docker-compose YAML, relative to
  `task.yaml`. This file is the sole source of truth for images, ports,
  volumes, health probes, and inter-service dependencies. The engine reads
  it verbatim.
- **`runner_service`** — which service in the compose file is the
  tolokaforge runner. The engine talks gRPC to this service to dispatch
  tool calls and grade the trial.
- **`isolation`** — whether the task tolerates state sharing across
  trials (`shared_ok`) or requires a fresh substrate per trial
  (`per_trial`). Default is `per_trial`. See [choosing isolation](#choosing-isolation)
  below.

### The compose file

The compose file lists the services the engine should bring up:

```yaml
# examples/native/multi_service/dataset/tasks/multi_service/multi_service_example_01/environment.compose.yaml
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

  app-service:
    image: nginx:1.27-alpine
    ports:
      - "80"
    volumes:
      - ./fixtures/products.json:/usr/share/nginx/html/products.json:ro
    healthcheck: ...
```

Three things worth noting:

- **`tolokaforge-runner:local` and `tolokaforge-db-service:local` are
  aliases** the engine sets up at run start. Task compose files reference
  these stable names instead of the content-hash tags the engine actually
  builds. Details in
  [`RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md).
- **Services reach each other by service name.** All services in a compose
  file join the same auto-generated docker network, so the runner container
  reaches `app-service` as `http://app-service/products.json`. No manual
  network wiring needed.
- **Pinned image tags are enforced.** The manifest validator rejects
  floating tags like `nginx:latest` — pin to a specific version
  (`nginx:1.27-alpine` above) so runs stay reproducible.

The engine validates a few more safety invariants when it loads the
manifest: no `network_mode: host`, no `privileged: true`, no `cap_add`,
`depends_on` must resolve, `runner_service` must be declared in the compose
file. Violations fail at task-load time with a clear error, not at trial
start.

## Choosing isolation

Two runtime backends decide *when* a stack materialises. Two isolation
values on the task decide *what the backend must satisfy*. The four
combinations aren't all supported:

| Task `isolation` | `--runtime shared` | `--runtime per_trial` |
| --- | --- | --- |
| `shared_ok` | ✅ Stack materialises once per run, all trials share it. Fastest. | ✅ Fresh stack per trial. Slower but stricter. |
| `per_trial` | ❌ Orchestrator refuses at startup — cross-trial contamination would break grading. | ✅ Fresh stack per trial. Required. |

Rule of thumb:

- If the task's grading only inspects the agent's output (files it wrote,
  a message it sent), and the services in the stack are read-only or
  reset-idempotent — declare `shared_ok`.
- If any trial mutates the environment in a way a later trial could observe
  (DB writes, filesystem edits, side-effects in a service) — declare
  `per_trial`. The orchestrator will enforce this by refusing an
  incompatible backend, so a grading bug from cross-trial contamination is
  a startup-time error, not a silent failure.

Default is `per_trial` — the safer choice. Opt into `shared_ok` only after
verifying grading is stateless.

## Adding another service

Take the working `multi_service` example and extend its compose file.
Suppose you want to add a redis cache:

1. Copy the example into your task pack:
   ```
   cp -r examples/native/multi_service my_pack/tasks/my_new_task
   ```
2. Add a `cache` service to `environment.compose.yaml`:
   ```yaml
   services:
     # ... existing runner, db-service, app-service ...
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
3. If the runner needs to reach it, add a `depends_on` and (optionally)
   an env var:
   ```yaml
   services:
     runner:
       environment:
         DB_SERVICE_URL: "http://db-service:8000"
         REDIS_URL: "redis://cache:6379"
       depends_on:
         cache:
           condition: service_healthy
   ```
4. Validate and run:
   ```bash
   uv run tolokaforge validate --tasks "my_pack/**/task.yaml"
   uv run tolokaforge run --config my_pack/run_config.yaml
   ```

The engine will materialise `runner`, `db-service`, `app-service`, and
`cache` for the run (or per trial, depending on the runtime you selected).

## Further reading

- [`examples/native/multi_service/README.md`](../../examples/native/multi_service/README.md)
  — the anchor example this guide walks through
- [`examples/native/multi_service_advanced/README.md`](../../examples/native/multi_service_advanced/README.md)
  — multi-endpoint aggregation across two task-specific HTTP APIs
- [`examples/native/multi_service_postgres/README.md`](../../examples/native/multi_service_postgres/README.md)
  — a realistic three-tier stack (PostgREST + postgres, no application
  code in the task pack)
- [ADR-0018](../architecture/adr/0018-multi-container-under-shared-runtime.md)
  — case matrix + sequence diagrams for each supported combination
- [`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md)
  — full lifecycle + materialisation deep-dive
- [`docs/TASKS.md`](../TASKS.md#multi-container-environments-environment_manifest)
  — reference schema for every `environment_manifest` field
