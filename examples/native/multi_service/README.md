# Multi-service example — Product Catalog Summary

A **task-declared multi-service** example. The task ships its own
`environment.compose.yaml` declaring four services (runner + db-service +
a stub `db` postgres required by the current endpoint resolver + a
task-specific `app-service`), and the engine materialises them **once at
run start** under `--runtime shared` (Case B in
[ADR-0018](../../../docs/architecture/adr/0018-multi-container-under-shared-runtime.md)).

The agent's job is to query the running product-catalog HTTP service and
write a short executive summary of the top-3 most expensive in-stock
products.

## What this example demonstrates

- Task-authored `environment_manifest` under **shared runtime** — realistic
  environment (real running HTTP service the agent hits) without paying
  per-trial substrate cost. Contrast with the per-trial isolation path,
  which materialises a fresh substrate per trial.
- Docker Compose service discovery inside the substrate. All services join
  the same auto-generated docker-compose network, so the runner container
  reaches `app-service` by service name via docker DNS
  (`http://app-service/products.json` from inside the runner).
- The `:local` engine-image alias pattern the engine applies at run start,
  so task compose files can reference `tolokaforge-runner:local` and
  `tolokaforge-db-service:local` regardless of the underlying content-hash
  tag (see `docs/architecture/RUNTIME_BACKENDS.md`).

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service/run_config.yaml
```

## Layout

```
examples/native/multi_service/
├── run_config.yaml                # models + orchestrator + evaluation
├── README.md                      # this file
└── dataset/tasks/multi_service/
    └── multi_service_example_01/
        ├── task.yaml              # declares environment_manifest + tools
        ├── environment.compose.yaml # 4-service compose
        ├── grading.yaml           # state checks + transcript rules
        └── fixtures/
            └── products.json      # served by app-service (nginx)
```

## Design notes

- **`isolation: shared_ok`** — the task tolerates state sharing across
  trials. Its grading only inspects the agent's output (a written file);
  the app-service is read-only static content that doesn't mutate.
- **`app-service` is deliberately trivial** — nginx serving a static JSON
  file. The point of the example is to demonstrate the multi-service
  materialisation path, not to be a realistic application. Real task
  packs would ship a proper backend + database + …
- **The `db` postgres service is a stub.** `db-service` uses an in-memory
  sqlite backend and does not talk to postgres, but the current
  shared+env_manifest endpoint resolver requires a compose service named
  `db` on port 5432 to satisfy `EnvEndpoints.db_url` construction. A
  follow-up ticket generalises endpoint resolution so this stub can go
  away.
- **The runner reaches `app-service` by service name.** Docker Compose
  auto-networks all services in a compose file; container-name-based DNS
  resolution works from any service to any other on the same network.

## Related

- [ADR-0018](../../../docs/architecture/adr/0018-multi-container-under-shared-runtime.md) — Case matrix + sequence diagrams for each supported case
- [ADR-0016](../../../docs/architecture/adr/0016-runtime-backend-comparison.md) — shared vs per_trial (lifecycle axis)
- [docs/architecture/RUNTIME_BACKENDS.md](../../../docs/architecture/RUNTIME_BACKENDS.md) — mechanics deep-dive
