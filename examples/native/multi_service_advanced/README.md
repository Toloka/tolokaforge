# Multi-service `advanced` example — Customer Value Analysis

The **advanced** counterpart to the sibling `multi_service` example. Adds two
things beyond the basic Case B demonstration:

1. **Two task-specific HTTP services**, not one. The agent must coordinate
   reads across an orders API and a customers API, join the two datasets on
   `customer_id`, and aggregate paid-order totals per customer before
   ranking.
2. **A different model tier for the agent.** The agent runs on Claude
   Haiku 4.5 (smaller, cheaper) while the user simulator stays on Sonnet 4.6.
   Useful for comparing multi-service handling across model classes.

Case B in [ADR-0018](../../../docs/adr/0018-multi-container-under-shared-runtime.md).

## What this example demonstrates

- **Multi-endpoint aggregation under shared runtime.** Four-service compose
  stack (runner + db-service + `orders-api` + `customers-api`),
  materialised once at run start and shared across every trial.
- **Docker Compose DNS discovery.** All four services join the same
  auto-generated docker-compose network, so the runner container reaches
  both APIs by service name (`http://orders-api/orders.json` and
  `http://customers-api/customers.json`).
- **Grading that pins the aggregate output.** Ranks Acme Robotics (12000) /
  Vector Industries (8500) / Nimbus Analytics (5000) as the top-3 by
  paid-order total — deterministic given the fixtures.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_advanced/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_advanced/run_config.yaml
```

## Layout

```
examples/native/multi_service_advanced/
├── run_config.yaml                # haiku-4.5 agent, sonnet-4.6 user
├── README.md                      # this file
└── dataset/tasks/multi_service/
    └── orders_customers_join_01/
        ├── task.yaml              # declares environment_manifest + tools
        ├── environment.compose.yaml # 4-service compose
        ├── grading.yaml           # state checks + transcript rules
        └── fixtures/
            ├── orders.json        # served by orders-api (nginx)
            └── customers.json     # served by customers-api (nginx)
```

## Design notes

- **`isolation: shared_ok`** — the task's grading only inspects the agent's
  written output; both APIs are read-only static content.
- **Both APIs use pinned nginx tags (`1.27-alpine`)** so runs are
  reproducible; the manifest validator rejects floating tags.

## Related

- [`../multi_service/README.md`](../multi_service/README.md) — the basic
  Case B example (single task-specific service, Sonnet 4.6 agent)
- [ADR-0018](../../../docs/adr/0018-multi-container-under-shared-runtime.md) — case matrix + sequence diagrams
