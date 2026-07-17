# Multi-service slow-start example

A **runnable** example that stress-covers Docker's start-order chain —
compose `depends_on: {condition: service_healthy}` + service
`healthcheck:` + `up --wait` — against a dependency that is deliberately
slow to become ready.

```
agent → PostgREST (HTTP API) → postgres:16 (real database, slow to accept TCP)
```

## What this example demonstrates

- **A genuinely slow dependency.** `app-db`'s
  `shared/app-db/init.sql` seeds the schema and a small real dataset and
  then runs `SELECT pg_sleep(25)` as its last statement. postgres runs
  init scripts on a temporary **socket-only** server and opens the real
  **TCP** listener only after the last script returns, so TCP :5432 is
  genuinely refused for ~25 s. This is the honest failure surface the
  startup-order race would hit — not a healthcheck that merely lies
  "starting" while postgres is actually ready.
- **The chain must hold the start order.** `app-service` (PostgREST)
  declares `depends_on: app-db {condition: service_healthy}`, and the
  per-trial backend brings the stack up with `up --wait`. If the chain
  did *not* hold, PostgREST would start before `app-db` accepts
  connections and the agent's first API call (runner → app-service →
  postgres) would hit a refused connection / 500.
- **A passing grade is the proof.** The agent reads widget 1's
  `name` (`slow_start_ok`) back over the REST API and writes it to a file
  under `submissions/`; grading asserts the file contains `slow_start_ok`.
  The agent can only have read that value if the chain gated the start
  order and the first call succeeded. A premature-connection failure
  would leave the submission empty and fail the state check.

## The load-bearing healthcheck

`app-db`'s healthcheck probes **TCP**, not the unix socket:

```yaml
test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U authenticator -d appdb"]
interval: 2s
timeout: 3s
retries: 15
start_period: 45s
```

- **`-h 127.0.0.1` (TCP), not the socket.** The temporary init server
  answers the unix socket during init. A socket-based `pg_isready` would
  flap healthy mid-`pg_sleep`, `--wait` would return early, and the slow
  start would never fire. The TCP probe fails while only the socket-only
  server is up and passes only once the real listener opens.
- **`start_period: 45s` sits above the ~25 s window.** Failing probes
  *during* `start_period` keep the container `starting` rather than
  `unhealthy` (which would fail `--wait`), so the stack waits out the
  slow start instead of erroring.

## Why per-trial (not shared)

`project.yaml` declares **no** `default_environment.services` block, so
the loader fills every compose service with the `ephemeral` default. That
makes the manifest's `requires_per_trial` true, so backend selection
routes the run onto the per-trial backend — the backend whose
`provision()` blocks on the full `depends_on` + healthcheck chain before
the trial's first RPC.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_slow_start/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_slow_start/run_configs/dev.yaml
```

Provisioning takes ≥25 s: the stack blocks on `app-db` becoming healthy,
which cannot happen until the `pg_sleep` in `init.sql` returns and the TCP
listener opens. Compose volumes are removed at teardown, so the slow init
runs fresh on every trial.

## Layout

```
examples/native/multi_service_slow_start/
├── project.yaml                       # Project spec: default_environment only (no services block, no assets)
├── run_configs/dev.yaml               # haiku agent + user, repeats: 1
├── README.md                          # this file
├── shared/
│   ├── environment.compose.yaml       # 4-service compose; app-db TCP healthcheck + wide start_period
│   └── app-db/
│       └── init.sql                   # schema + seeded data + trailing pg_sleep(25) (docker-entrypoint-initdb.d)
└── dataset/tasks/startup_probe/
    ├── task.yaml                       # inherits the project default_environment whole; no environment_manifest
    └── grading.yaml                    # deterministic state check on the written submission
```

## Design notes

- **No reset recipe, no seed asset.** This pack is about start *order*,
  not between-trial reset. `app-db` carries no `isolation: reset` and the
  project declares no `assets.seeds`. A reset asset runs *after*
  `up --wait` returns, so it would not delay the chain under test.
- **`pg_sleep` over a large import.** A `pg_sleep(25)` makes postgres
  genuinely TCP-unreachable for a hardware-independent, wall-clock-bounded
  window, so the ≥20 s slow-start floor is stable across machines. A
  data-volume-only delay would vary by hardware and flake. The seeded
  `generate_series` dataset keeps the queried DB a real one.
- **The task declares no `environment_manifest`.** It inherits the
  project's `default_environment` whole. A task-level `stack.compose_file`
  would atomically replace the stack and drop the project's services map.
- **Deterministic grading, no judge.** The pass/fail signal is a state
  check that the submission contains `slow_start_ok`; correctness does not
  depend on an LLM grader.
- **Pinned real images.** `postgrest/postgrest:v12.2.0` and `postgres:16`;
  the engine images are referenced as `tolokaforge-runner:local` /
  `tolokaforge-db-service:local`.

## Related

- [`../multi_service_postgres_reset/README.md`](../multi_service_postgres_reset/README.md) —
  the reset-recipe example this pack's compose stack mirrors (minus the
  reset asset and labels).
