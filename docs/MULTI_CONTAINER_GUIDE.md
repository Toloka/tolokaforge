# Multi-container tasks

This guide walks through authoring a project whose tasks ship their own
docker-compose stack — extra services beyond the engine's built-in
`runner` + `db-service`. It's anchored to a working example
([`examples/native/multi_service_postgres_reset/`](../examples/native/multi_service_postgres_reset/))
that you can `tolokaforge run` unchanged before adapting it.

For the design rationale + case matrix, see
[ADR-0018](adr/0018-multi-container-under-shared-runtime.md).
For the full Project model, see
[`docs/PROJECTS.md`](PROJECTS.md); for the
runtime backend lifecycle, see
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md).

**Who this is for:**

- **Want to see it work?** Skip to [Running the examples](#running-the-examples),
  pick any pack, and run its `run_configs/<name>.yaml`.
- **Want to author a new multi-service task?** Read the
  [Walkthrough](#walkthrough--multi_service_postgres_reset) below; it walks the
  smallest real example end-to-end.

## Running the examples

If you want to see the machinery work before authoring your own stack, the
repo ships six multi-container packs that run end-to-end with one command
each. The first four form a ladder — start at the top for the simplest shape,
drop to the flagship; the last two are scenarios on different axes — a
debugging investigation and an auto-dev build-and-verify loop.

| Pack | What makes it different |
| --- | --- |
| [`multi_service_postgres`](../examples/native/multi_service_postgres/README.md) | The primer. A single postgres behind a PostgREST API — the simplest real three-tier stack, no application code to author (PostgREST generates the REST endpoints from the schema). |
| [`multi_service_postgres_reset`](../examples/native/multi_service_postgres_reset/README.md) | Adds the reset-recipe pattern: a `reset` postgres service re-seeded from a named `sql_dump` seed at the start of every trial. |
| [`multi_service_lot_ops`](../examples/native/multi_service_lot_ops/README.md) | The first pack to grade the substrate. The agent mutates postgres over a FastAPI API; `state_checks.db_probes` verifies the row directly through a read-only `grader` role — an independent oracle, not the API the agent wrote through. |
| [`multi_service_helpdesk_workflow`](../examples/native/multi_service_helpdesk_workflow/README.md) | The flagship. Four business services + a policy-search service backed by a postgres-FTS corpus. The agent must reconcile customer, product, site, and policy data to pick the one policy-valid resolution of three plausible paths; a wrong path grades down even with a well-formed CRM row. Declares its postgres substrate `ephemeral` explicitly and formalises the LLM user-simulator persona pattern. |
| [`multi_service_cache_debug`](../examples/native/multi_service_cache_debug/README.md) | The debugging scenario, and the `redis_dump` reset reference. A `redis` service `isolation: reset` is re-seeded each trial from an RDB carrying poisoned cache state; the agent diagnoses why the orders API serves stale reads across the app + cache layers and writes a root-cause note, graded three ways (`state_checks` + `transcript_rules` + `llm_judge`). A grade-fail exercises the per-service log capture. |
| [`multi_service_endpoint_add`](../examples/native/multi_service_endpoint_add/README.md) | The auto-dev scenario, and the `filesystem_dir` reset reference. A source-directory `testrunner` service `isolation: reset` is re-seeded each trial from a pristine FastAPI source tree over a volume shared with the agent's `/work`; the agent reads the code, writes a missing `GET /orders/{id}/summary` endpoint, and runs the real suite over `http_request` to the test-runner, whose actual `unittest` exit code is the decisive grading floor. |

### The command

Every pack runs the same way — point `tolokaforge run` at its run config:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_postgres/run_configs/dev.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_postgres_reset/run_configs/dev.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_lot_ops/run_configs/dev.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_helpdesk_workflow/run_configs/dev.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_cache_debug/run_configs/dev.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_endpoint_add/run_configs/dev.yaml
```

`scripts/with_env.sh` loads `.env` (so `OPENROUTER_API_KEY` reaches the run)
before invoking the CLI. Prerequisites for every pack:

- The `uv` toolchain — `curl -LsSf https://astral.sh/uv/install.sh | sh`, then
  `uv sync` from the repo root to install dependencies.
- A running Docker daemon — `docker version` must return cleanly.
- `OPENROUTER_API_KEY` in `.env`; every pack drives real models.

The engine images `tolokaforge-runner:local` and `tolokaforge-db-service:local`
are built automatically on the first `tolokaforge run` (and cached thereafter),
so there is no separate build step — see
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md).

Each run writes to the `output_dir` named in its `run_configs/<name>.yaml` (under
`results/`). To validate a pack's tasks without running them, use
`uv run tolokaforge validate --tasks "<pack>/dataset/**/task.yaml"`.

**Cost expectations** (a single trial each, honest ballpark — check each
pack's `run_configs/<name>.yaml` for the exact models):

| Pack | Models | Rough cost |
| --- | --- | --- |
| `multi_service_postgres` | Sonnet agent + user, 15 turns | ~$1–3 |
| `multi_service_postgres_reset` | Haiku agent + user, `repeats: 2` | ~$0.15–0.40 |
| `multi_service_lot_ops` | Haiku agent + user, Sonnet judge | ~$0.30–1.00 |
| `multi_service_helpdesk_workflow` | Haiku agent + user, Sonnet judge, 18 turns | ~$0.50–1.50 |
| `multi_service_cache_debug` | Haiku agent + user, Sonnet judge, 20 turns | ~$0.50–1.50 |
| `multi_service_endpoint_add` | Haiku agent + user, Sonnet judge, 25 turns, `repeats: 2` | ~$1–3 |

### What to look at in the output

Every trial lands under `<run_dir>/trials/<task_id>/<trial_index>/`.

**When a run fails, look at `services/<name>.log` first** — it is the raw
`docker compose logs` for each service and points straight at the container
that misbehaved. The files worth opening after a run:

- **`grade.yaml`** — the verdict: `binary_pass`, `score`, the per-family
  `components` breakdown, and a `reasons` string. For a pack that uses
  `db_probes`, `reasons` carries a `DB probes: …` segment (`DB probes: all
  probes passed`, or the failing assertion). When an LLM judge ran,
  `criterion_results` gives the per-criterion rubric breakdown.
- **`env.yaml`** — for a manifest-driven trial this carries an
  `environment:` block recording the resolved substrate: `network_policy`,
  `runner_service`, and a per-service `services:` map with each service's
  resolved image (and whether it's `pinned`), `isolation`, `reset_seed`,
  `dsns` (passwords redacted to `***`), and container-side `mounts` (host
  source paths omitted). It is a pure function of the resolved manifest, so
  it survives per-trial teardown and tells a post-mortem exactly which
  image / DSN / mounts a trial ran against without leaking secrets.
- **`services/<name>.log`** — raw `docker compose logs` for each service,
  written on the per-trial backend when a trial is diagnostics-worthy: it
  fails to provision, its body errors/times out, **or** it runs to
  completion but grades red. One file per service that produced output; the
  tail bound is `compute.log_tail` (default 500 lines). A passing trial does
  not trigger capture — set `compute.capture_logs_on_success: true` (see
  [`docs/CONFIG.md`](CONFIG.md)) to keep the service logs of green runs too.
  This makes post-mortem on a red integration run trivial — the service that
  misbehaved is right there.
- **`trajectory.yaml`** and **`metrics.yaml`** — the message transcript and
  the per-trial usage / cost / tool-call telemetry. Both are documented in
  [`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md); `metrics.yaml` also amends a
  `captured_service_logs` byte-count map when service logs are captured.

### How the pieces fit

A multi-container run composes five layers:

1. **The task pack declares an `environment_manifest`** — a compose file
   plus a per-service isolation map (`shared` / `reset` / `ephemeral`).
   This is the sole source of truth for the topology.
2. **The runtime backend materialises the stack.** Isolation drives the
   choice automatically: any `reset`/`ephemeral` service routes the run to
   the per-trial backend (fresh stack per trial, reset recipes applied);
   an all-`shared` manifest uses the shared-stack backend (materialised
   once). No flag selects this — the tasks do.
3. **The agent runs inside the runner container** and reaches the other
   services over the internal compose network by service name (e.g.
   `http://policy-search:8000`). `network_policy` governs public egress.
4. **Grading blends independent signals** — `state_checks.db_probes`
   (an independent postgres oracle via a read-only role) with
   `transcript_rules.required_actions` (did the agent take the right tool
   actions), `trace_checks` (did it take them in the right order) and an
   `llm_judge` rubric, combined by weight.
5. **Traces land in the trial dir** — `grade.yaml`, `trajectory.yaml`,
   `metrics.yaml`, `env.yaml`, and (on failure) per-service logs — the full
   post-mortem surface for one trial.

### Where to go next

- [`docs/PROJECTS.md`](PROJECTS.md) — the Project
  schema: `assets`, `default_environment`, per-service isolation, merge chains.
- [`docs/GRADING.md`](GRADING.md) § Substrate Grading — the full
  `state_checks.db_probes` field reference and aggregation rules.
- [`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) — every trial artifact in detail.
- [`docs/TASKS.md`](TASKS.md) § User Simulator — the specialised-persona
  pattern the helpdesk pack uses for its LLM user simulator.

The rest of this guide is the authoring reference: how to declare a stack,
choose isolation, write reset recipes, and grade against substrate state.

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
[`examples/native/multi_service_postgres_reset/`](../examples/native/multi_service_postgres_reset/).
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
- **`stack.{runner_port, db_service, db_port, rag_service, rag_port}`** —
  optional endpoint overrides for compose files that deviate from the
  well-known service names and ports. See
  [endpoint overrides](#endpoint-overrides) below.
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
        condition: service_started
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
  [`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md).
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

### Endpoint overrides

The engine addresses three services in your stack by convention: the runner
over gRPC, an HTTP state backend (`db-service`), and an optional RAG service.
The convention defaults cover the packs that use the engine's built-in
service names, so most tasks set nothing here. When your compose file names a
service differently or publishes it on a non-standard port, override the
convention from the `stack:` block:

| Field | Default | Override when… |
| --- | --- | --- |
| `runner_port` | `50051` | the runner service listens on a different gRPC port |
| `db_service` | `db-service` | the HTTP state backend is a differently-named service |
| `db_port` | `8000` | that backend publishes a different port |
| `rag_service` | `rag` / `rag-service` (scanned) | the RAG service is named something else |
| `rag_port` | first published port | you want to pin the RAG container port |

```yaml
default_environment:
  stack:
    compose_file: ./shared/environment.compose.yaml
    runner_service: agent
    runner_port: 6000
    db_service: state-backend
    db_port: 9000
```

`db_service` and `rag_service` are validated at load: naming a service that
the compose file does not declare fails with a clear `ValidationError`, so a
typo never silently degrades to a missing endpoint at runtime. The ports and
the RAG scan are best-effort — an unresolved port leaves the endpoint unset,
exactly as the convention default does. A task that swaps
`stack.compose_file` replaces the stack atomically and clears any
project-level endpoint overrides along with it.

See [ADR-0009](adr/0009-environment-manifest.md) for the design rationale
behind individual scalar fields over a uniform endpoint map.

## Choosing isolation

Isolation is declared **per service**, under
`services.<name>.isolation`. There are three values:

| `isolation` | Between trials | Use when |
| --- | --- | --- |
| `shared` | The container persists; all trials share it. | The service is long-lived and stateless, or its state is meant to accumulate across trials. Fastest. |
| `reset` | A fresh container per trial, with a named seed applied at each provision. | The agent mutates the service (DB writes, side-effects) and each trial must start from a known baseline. |
| `ephemeral` | A fresh container per trial, no seed. | The agent mutates the service and a clean substrate — not a specific seed — is enough. This is the **default** for any service without an entry. |

Backend selection is **automatic**: any `reset`/`ephemeral` service routes the
run to the per-trial backend, an all-`shared` manifest to the shared-stack
backend — no flag selects it. See
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md#isolation-enforcement)
§ Isolation enforcement for how that choice is derived and enforced.

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
[`docs/RESET_RECIPES.md`](RESET_RECIPES.md).

## Network policy

`network_policy` sets the egress posture for task services. The default is
`no_internet`: task services have no public egress, while the runner keeps
an edge network so LLM-judge grading can still reach model providers.

`full_internet` is the explicit opt-in for a task that legitimately needs
unrestricted egress (fetching an arbitrary remote resource, calling any
third-party API under test).

`limited_internet` sits between the two: application services may reach a
declared allowlist of hosts and nothing else. Declaring it requires a
`limited_internet_allowlist` on the same `stack:` block; the two are validated
together at load time — `limited_internet` with an empty allowlist is a load
error, and a non-empty allowlist under any other policy is a load error.

```yaml
default_environment:
  stack:
    compose_file: ./shared/environment.compose.yaml
    runner_service: runner
    limited_internet_allowlist:
      - api.openai.com        # exact host — only this hostname
      - "*.githubusercontent.com"  # wildcard — any subdomain of githubusercontent.com
  network_policy: limited_internet
```

Allowlist entry syntax:

- **Bare hostname** (`api.openai.com`) — exact match. Only that hostname is
  reachable; subdomains are not.
- **Leading wildcard** (`*.example.com`) — subdomain suffix match: any host
  ending in `.example.com` (e.g. `raw.example.com`, `cdn.assets.example.com`).
  The bare apex `example.com` is *not* matched by `*.example.com` — list it
  separately if you need it.

Entries are DNS hostnames only. Schemes (`http://…`), embedded ports
(`host:443`), URL paths, IP literals (`10.0.0.1`), and duplicate entries are
all rejected at manifest load.

How it is enforced: the provisioner injects a digest-pinned `ubuntu/squid`
forward-proxy sidecar and points every application service's `HTTP(S)_PROXY`
(unless opted out via `network_access: restricted`) at it. The proxy is default-deny and forwards only to allowlisted hosts;
everything else is refused with HTTP 403. HTTPS egress goes through the proxy
via CONNECT tunnelling with no TLS interception — the allowlist matches on the
target hostname, so pinned certificates keep working and there is no CA to
install in the service's trust store. The `runner_service` is not proxied: it
keeps direct edge egress for LLM-judge grading, exactly as under `no_internet`.
For the full design see
[ADR-0018](adr/0018-multi-container-under-shared-runtime.md#network-policy-enforcement).

### Partitioning an untrusted sibling

`network_policy` governs *public* egress; by default, application services
under `no_internet` and `limited_internet` share the harness-injected
`tolokaforge_netpolicy_internal` network, so any service can DNS-resolve and
dial any other on port paths the compose file exposes (e.g. an untrusted
`bash` sibling could `curl http://db-service:8000/update` or
`grpcurl runner:50051 ExecuteTool`). When one sibling in the stack is
untrusted — an agent-controlled shell whose only intended egress is a curated
tool-bridge service, for example — mark it `network_access: restricted` in
the manifest so it joins only the networks its compose entry declares.

Shape:

1. Declare a task network in the compose file and list only the services the
   untrusted sibling is allowed to talk to on it:

   ```yaml
   # environment.compose.yaml
   services:
     runner:
       image: tolokaforge-runner:local
       # ...
     tool-bridge:
       image: my-org/tool-bridge:v1.0.0
       networks: [tool_bridge]
     bash:
       image: bash:5.2-alpine3.20
       networks: [tool_bridge]
   networks:
     tool_bridge: {}
   ```

2. Mark the untrusted sibling `restricted` in the manifest:

   ```yaml
   # project.yaml (or task.yaml environment_manifest)
   default_environment:
     stack:
       compose_file: ./environment.compose.yaml
       runner_service: runner
     services:
       bash:
         isolation: ephemeral
         network_access: restricted
     network_policy: no_internet
   ```

Guarantees the harness enforces on the restricted service:

- **No shared-internal-net attach.** The service does not join
  `tolokaforge_netpolicy_internal`; it cannot resolve or dial any sibling
  (runner, db-service, rag) via the harness-injected network.
- **Task-declared networks are still `internal: true`.** The `tool_bridge`
  network above stays egress-blocked at the docker-network level, so the
  restricted service still has no public internet — the compose file's own
  `networks:` topology is the positive expression of "who this service *can*
  talk to".
- **No proxy env under `limited_internet`.** A restricted service receives no
  `HTTP_PROXY` / `HTTPS_PROXY` (nor lowercase variants); the squid sidecar is
  on the harness-injected nets the restricted service does not join, so the
  proxy is unreachable by design. An injected `HTTP_PROXY` would produce
  confusing "connection refused" symptoms rather than the fail-loud isolation
  the operator asked for.

Two invariants fail loud at manifest load:

- **The runner cannot be `restricted`.** The runner must remain reachable
  from application services and must keep its injected edge network for
  LLM-judge grading.
- **A `restricted` service's compose entry must declare its own `networks:`
  block.** A restricted service with no declared networks would attach to
  nothing under `no_internet` / `limited_internet` and never come up — the
  validator refuses the shape at load rather than surface a mysterious
  docker error later.

Reachability is topological, not authorial: if the compose file joins the
restricted service to a network that a first-party service (db-service,
runner) also joins, the task author has *granted* that reachability by
construction. See also [`SECURITY.md`](SECURITY.md).

## Adding another service

Starting a brand-new pack instead of adapting the walkthrough? See
[`docs/PROJECTS.md`](PROJECTS.md#the-three-pieces-of-a-project)
§ "The three pieces of a project" for the scaffold shape.

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
   scripts/with_env.sh uv run tolokaforge run --config my_project/run_configs/dev.yaml
   ```

Because `cache` is `reset` (and `app-db` already is), the run requires
per-trial isolation and routes to `PerTrialRuntimeBackend` automatically.

## Grading against substrate state

When the agent *mutates* a service — writes a row, updates a record — grade
the mutation against the substrate directly rather than trusting the agent's
own written file. `state_checks.db_probes` connects to a task-declared postgres
DSN, runs an author-written read-only `SELECT`, and applies the JSONPath
assertion vocabulary to the returned rows. Point the DSN at a dedicated
read-only role (`GRANT SELECT` only) so the probe is an **independent oracle**:
it reads the database through a different role than the API the agent wrote
through, and it can never mutate the substrate. The runner container joins the
task's docker network, so it reaches the service (e.g. `app-db:5432`) at grade
time.

Substrate state is one of four grader families these packs blend:

- **`state_checks.db_probes`** — the independent-oracle read against the
  substrate described above.
- **`transcript_rules.required_actions`** — asserts the agent actually took the
  named tool actions during the run (e.g. called a specific endpoint), grading
  the process rather than only the end state.
- **`trace_checks`** — declarative conditions on the trajectory itself: ordering,
  scoped absence, and counting over the trial's event timeline, for the process
  claims a flat presence check cannot state (e.g. the payment was looked up
  *before* the case was denied).
- **`llm_judge`** — a rubric scored by a judge model, for open-ended output a
  deterministic check can't express (a root-cause note, a well-argued rationale).

Full field reference for all four in [`docs/GRADING.md`](GRADING.md) §
Substrate Grading; the `multi_service_lot_ops` pack below is the worked example.

## Further reading

- [`examples/native/multi_service_postgres_reset/README.md`](../examples/native/multi_service_postgres_reset/README.md)
  — the anchor example this guide walks through (per-service isolation +
  `sql_dump` reset seed)
- [`examples/native/multi_service_slow_start/README.md`](../examples/native/multi_service_slow_start/README.md)
  — startup-order stress: a slow dependency that the orchestrator waits on
  via healthcheck before the runner fires
- [`examples/native/multi_service/README.md`](../examples/native/multi_service/README.md)
  — the task-level shared multi-container pattern (nginx catalog)
- [`examples/native/multi_service_postgres/README.md`](../examples/native/multi_service_postgres/README.md)
  — a realistic three-tier stack (PostgREST + postgres, shared runtime)
- [`examples/native/multi_service_lot_ops/README.md`](../examples/native/multi_service_lot_ops/README.md)
  — substrate-state grading: the agent mutates postgres over a FastAPI API and
  `state_checks.db_probes` verifies the row directly via a read-only role
- [`examples/native/multi_service_helpdesk_workflow/README.md`](../examples/native/multi_service_helpdesk_workflow/README.md)
  — flagship cross-service pack: four FastAPI services + in-container
  postgres-FTS policy search, an adversarial three-path resolution graded on
  policy correctness, an explicit `ephemeral` substrate, and the specialised
  user-simulator persona pattern
- [`examples/native/multi_service_cache_debug/README.md`](../examples/native/multi_service_cache_debug/README.md)
  — the runnable `redis_dump` reset reference: a `reset` redis service
  re-seeded from a poisoned-cache RDB each trial, a cache-invalidation bug the
  agent diagnoses across the app + cache layers, three-way note grading, and a
  grade-fail that exercises per-service log capture
- [`examples/native/multi_service_endpoint_add/README.md`](../examples/native/multi_service_endpoint_add/README.md)
  — the runnable `filesystem_dir` reset reference: a `reset` source-directory
  `testrunner` service re-seeded from a pristine FastAPI source tree over a
  volume shared with the agent's `/work`, an auto-dev loop where the agent adds
  a missing endpoint and runs the real suite over HTTP, and test-execution
  grading whose decisive floor is the suite's actual exit code
- [`examples/native/example-microservices-pack/`](../examples/native/example-microservices-pack/)
  — the schema reference pack: full inheritance/override matrix across five
  tasks (reference only, see its README before running)
- [`docs/PROJECTS.md`](PROJECTS.md)
  — the full Project model: assets, `default_environment`, per-service
  isolation, and the merge chains
- [`docs/RESET_RECIPES.md`](RESET_RECIPES.md)
  — the four seed kinds and how a reset recipe is applied
- [ADR-0018](adr/0018-multi-container-under-shared-runtime.md)
  — case matrix + sequence diagrams for each supported combination
- [`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md)
  — full lifecycle + materialisation deep-dive
