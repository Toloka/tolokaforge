# Runtime backends — how a trial actually runs

This document walks through the `RuntimeBackend` seam end-to-end: what the
Protocol demands, how each production implementation satisfies it, what
happens over the lifetime of a single trial, and how per-trial isolation
composes with the compose-as-source-of-truth manifest.

Companion reading: ADR-0007 (`RuntimeBackend` Protocol), ADR-0010 (per-trial
provisioning contract), ADR-0013 (per-trial RPC methods on the backend),
ADR-0009 (`EnvironmentManifest`), ADR-0011 (Pattern B addendum — typed
wrapper over an external artifact).

## The seam

`RuntimeBackend` is the orchestrator's execution surface. A single Protocol,
nine methods, three concerns:

| Group | Methods | Called when |
|---|---|---|
| Run-level lifecycle | `connect`, `close`, `health_check` | Once per orchestrator run |
| Per-trial provisioning (ADR-0010) | `provision`, `await_ready`, `endpoints`, `teardown` | Around every trial body |
| Per-trial RPC (ADR-0013) | `register_trial`, `execute_tool`, `grade_trial`, `get_state`, `reset_trial`, `cleanup_trial` | Inside the trial body |

Every implementation ships all nine. The orchestrator holds one backend
instance for the whole run; the conductor calls it per-trial.

```mermaid
graph TB
    subgraph Process["Orchestrator process"]
        Orch[Orchestrator]
        Cond[Conductor]
        Orch --> Cond
        Cond --> RB{"RuntimeBackend<br/>Protocol"}
    end

    RB -.implements.-> DR["SharedStackRuntimeBackend<br/>(shared stack)"]
    RB -.implements.-> LRB["PerTrialRuntimeBackend<br/>(per-trial)"]
    RB -.implements.-> IMB["InMemoryRuntimeBackend<br/>(tests only)"]

    subgraph Docker["Docker daemon"]
        SS[["Shared stack<br/>one project<br/>db + runner + rag"]]
        T1[["Per-trial stack #1<br/>own project<br/>db + runner"]]
        T2[["Per-trial stack #2<br/>own project<br/>db + runner"]]
    end

    DR -->|"one shared<br/>GrpcRunnerClient"| SS
    LRB -->|"one client per trial<br/>keyed by trial_id"| T1
    LRB -->|"one client per trial<br/>keyed by trial_id"| T2
```

Selection is a run-level config choice: `SharedStackRuntimeBackend` is the default
(preserves today's behaviour); a task that declares an `environment_manifest`
runs on `PerTrialRuntimeBackend` when the orchestrator opts in. That wiring is
the next PR (Conductor + Orchestrator injection); this PR ships the backend.

## Concrete backends

**`SharedStackRuntimeBackend`** — the original. One compose project brought up at
`connect()` time and shared across every trial in the run. Per-trial
"isolation" is `trial_id`-in-URL only; cross-trial state contamination is
structural. `provision` / `teardown` are no-ops on the run-wide stack. Fine
for single-tenant, sequential runs; caps concurrency at one trial per
service that has stateful side effects.

**`PerTrialRuntimeBackend`** — this PR. One compose project per trial via
`testcontainers.compose.DockerCompose`. Each `provision` call materialises
an isolated stack; the trial's runner container is only reachable through
its own network + host-side port. Concurrent trials each get independent
containers, networks, and volumes. Backwards-compatible because it is
opt-in: tasks that do not declare an `environment_manifest` still run on
`SharedStackRuntimeBackend`.

**`InMemoryRuntimeBackend`** — test-only. Records every method call on a
`RuntimeBackendCallLog`; no Docker daemon required. Used by canonical
contract tests.

## A trial's lifecycle on `PerTrialRuntimeBackend`

The following sequence covers one trial end-to-end. Reads left-to-right in
time; each arrow is a real method call on the class instances named at the
column headers.

```mermaid
sequenceDiagram
    autonumber
    participant Orch as Orchestrator
    participant Cond as Conductor
    participant LRB as PerTrialRuntimeBackend
    participant DC as Testcontainers<br/>DockerCompose
    participant GRC as GrpcRunnerClient<br/>(this trial's)
    participant Docker as Docker daemon

    Note over Orch,LRB: Run start (once)
    Orch->>LRB: connect()
    Note right of LRB: no-op — no shared runner

    Note over Cond,LRB: Per trial (each call ↓)
    Cond->>LRB: provision(spec)

    activate LRB
    LRB->>LRB: make per-trial temp dir<br/>(embeds trial_id)
    LRB->>LRB: copy compose file<br/>+ sibling files into it
    LRB->>DC: new DockerCompose(<br/> context=temp_dir,<br/> wait=True)
    LRB->>DC: .start()
    DC->>Docker: docker compose up -d --wait
    Docker-->>DC: containers up +<br/>healthchecks pass
    LRB->>DC: get_service_host_and_port(<br/> runner_service, 50051)
    LRB->>DC: get_service_host_and_port(<br/> "db", 5432)
    LRB->>DC: rag lookup (optional)
    LRB->>GRC: new GrpcRunnerClient(<br/> host:port)
    Note right of GRC: constructed —<br/>NOT connected
    LRB->>LRB: cache client, snapshot endpoints on handle
    LRB-->>Cond: _LocalEnvHandle
    deactivate LRB

    Cond->>LRB: endpoints(handle)
    LRB-->>Cond: handle.endpoints<br/>(pure read)

    Note over Cond,GRC: First RPC — connect happens now
    Cond->>LRB: register_trial(trial_id, ...)
    LRB->>LRB: _client_for(trial_id)
    LRB->>GRC: .connect(timeout=30)
    GRC->>Docker: gRPC health check loop
    Docker-->>GRC: healthy
    LRB->>LRB: add trial_id to _connected_trials
    LRB->>GRC: .register_trial(...)
    GRC->>Docker: RegisterTrial RPC
    GRC-->>LRB: result
    LRB-->>Cond: result

    Note over Cond,GRC: Subsequent RPCs — client already connected
    Cond->>LRB: execute_tool(trial_id, ...)
    LRB->>GRC: .execute_tool(...)
    Cond->>LRB: get_state(trial_id, ...)
    LRB->>GRC: .get_state(...)
    Cond->>LRB: grade_trial(trial_id, ...)
    LRB->>GRC: .grade_trial(...)

    Note over Cond,Docker: Teardown (finally block)
    Cond->>LRB: teardown(handle)
    LRB->>GRC: .close()
    LRB->>DC: .stop(down=True)
    DC->>Docker: docker compose down -v
    LRB->>LRB: shutil.rmtree(temp_dir)
    LRB->>LRB: pop client from cache,<br/>discard from connected set

    Note over Orch,LRB: Run end
    Orch->>LRB: close()
    Note right of LRB: closes any leftover<br/>connected clients
```

Every step above is a single method call in `tolokaforge/core/per_trial_runtime.py`.

## Deep-dive — `provision()`

Nine steps, in order. Failure at any step raises `ProvisionError(stage="provision")` and cleans up whatever ran successfully before the raise.

1. **Guard on manifest presence.** `spec.task.environment_manifest is None` → raise. Tasks without a manifest belong on `SharedStackRuntimeBackend`, not this backend.
2. **Make a per-trial temp directory.** Path like `/tmp/tolokaforge-<sanitised-trial-id>-<random>/`. The basename is what Docker Compose reads for its auto-generated project name — encoding the trial id here is what gives each concurrent trial its own project.
3. **Copy the compose context.** Everything in the compose file's parent directory (compose YAML, adjacent bind-mount source files, initial-state fixtures) copies into the temp dir. Bind mounts declared as relative paths resolve inside the copied context; safety validators (ADR-0009) already reject `..` and absolute paths, so the copy is closed and complete.
4. **Construct `DockerCompose`** with `context=<temp_dir>`, `compose_file_name=<manifest.compose_file.name>`, `pull=False`, `build=False`, `wait=True`.
5. **`compose.start()`.** Runs `docker compose up -d --wait`. Blocks until every service's compose `healthcheck:` reports healthy. On failure, raise `ProvisionError` and rmtree the temp dir.
6. **Construct the runner client** — `GrpcRunnerClient(runner_address="<host>:<port>")`. Host + port come from `compose.get_service_host_and_port(manifest.runner_service, 50051)`. **The client is not connected here** — `connect()` is deferred to first RPC use (see next section).
7. **Snapshot endpoints on the handle.** `_resolve_endpoints(...)` looks up `runner_service` at 50051 and the conventional `db` service at 5432, plus an optional `rag` service. Missing `db` raises `ProvisionError` (see follow-up ticket for making this customisable).
8. **Cache the client** — `self._clients[spec.trial_id] = client`. This is the map every per-trial RPC method reads from.
9. **Return `_LocalEnvHandle`** carrying the trial_id (public), the compose stack, the runner service name + port, the temp dir, and the endpoints snapshot. All except `trial_id` are backend-private; callers treat the handle as an opaque token.

## Endpoint resolution

`endpoints(handle)` is a **pure read** — it returns `handle.endpoints`, resolved once at provision time. This is a deliberate departure from an earlier design where `endpoints()` re-queried the compose stack every call: a method named `endpoints` mutating state on missing-service was surprising, and the missing-db check now fails fast at provision time before a handle exists.

Where each URL comes from (defaults; customisation is a follow-up):

| Field | Source | Port |
|---|---|---|
| `runner_url` | `manifest.runner_service` (default `"default"`) | `50051` |
| `db_url` | compose service named **`db`** | `5432` |
| `rag_url` | compose service named `rag` or `rag-service`; else `None` | service's declared port |

All three are resolved via `compose.get_service_host_and_port(name, port)`, which returns the host-assigned port that maps to a service's container port. Two concurrent trials of the same task get **different** host-assigned ports for the same container port — that is what makes concurrent stacks not clash.

## Per-trial isolation

Testcontainers' `DockerCompose` does not accept a `project_name` parameter. Docker Compose derives the project name from the context directory basename by default. `PerTrialRuntimeBackend` leverages that: each trial's compose file is copied into a temp directory whose name embeds the trial id, so each trial's `DockerCompose` instance sees a unique project name.

```mermaid
graph TB
    subgraph Trial1["Trial task-1:0"]
        TD1["temp dir<br/>tolokaforge-task-1_0-abc123"]
        DC1["DockerCompose<br/>project=tolokaforge-task-1_0-abc123"]
        C1a["container:<br/>db"]
        C1b["container:<br/>runner"]
        N1["network:<br/>_default"]
        V1["volumes"]
        TD1 --> DC1
        DC1 --> C1a
        DC1 --> C1b
        DC1 --> N1
        DC1 --> V1
    end
    subgraph Trial2["Trial task-1:1"]
        TD2["temp dir<br/>tolokaforge-task-1_1-def456"]
        DC2["DockerCompose<br/>project=tolokaforge-task-1_1-def456"]
        C2a["container:<br/>db"]
        C2b["container:<br/>runner"]
        N2["network:<br/>_default"]
        V2["volumes"]
        TD2 --> DC2
        DC2 --> C2a
        DC2 --> C2b
        DC2 --> N2
        DC2 --> V2
    end
    subgraph Docker["Docker daemon"]
    end
    Trial1 -.-> Docker
    Trial2 -.-> Docker
```

Same compose file → two independent projects → independent networks (no cross-trial reachability), independent volumes (no cross-trial state), independent host ports (no port collision).

## Lazy runner-client connect

`GrpcRunnerClient.connect()` runs a gRPC health-check retry loop (up to 30s
by default). For a run-wide backend like `SharedStackRuntimeBackend`, that cost is
amortised — one connect at run start covers every trial. For a per-trial
backend, connecting eagerly at `provision()` time would add the connect cost
to every trial's provisioning latency, even for trials that never actually
call an RPC (e.g., a trial that fails inside the provision path but not
inside the runner).

The industry pattern is lazy: gRPC channels connect on first RPC call; boto3,
`kubernetes-client`, and the Docker SDK all construct lazily; Testcontainers
itself separates "container up" (via `--wait`) from "application-level
connect" (caller's problem).

`PerTrialRuntimeBackend` follows suit:

- `provision()` constructs the `GrpcRunnerClient` but does not call `.connect()`.
- The `_connected_trials: set[str]` tracks which trials' clients have already been through their connect health check.
- `_client_for(trial_id)` — invoked by every per-trial RPC method — checks the set; if the trial isn't in it, calls `.connect()` once and adds it. Subsequent calls to the same trial's RPCs skip the connect.
- `teardown(handle)` and `close()` only invoke `.close()` on clients that were actually connected — closing a never-used client would have nothing to close on the gRPC side.

This means the compose `--wait` gate (which already blocks until every container reports healthy) is the only latency `provision()` adds beyond the compose CLI itself. First RPC call pays the connect cost; subsequent calls hit a warm client.

## Teardown + cleanup

`teardown(handle)` is idempotent, per ADR-0010. It performs, in order:

1. Pop the client from `_clients` (returns None if already torn down).
2. Discard the trial id from `_connected_trials`; capture whether it was in the set (that determines whether the client needs closing).
3. If the client existed AND was connected, call `.close()` on it. Best-effort — logs on failure, does not raise.
4. `_shutdown_compose(handle.compose)` — runs `compose.stop(down=True)`, which is `docker compose down -v` under the hood. Best-effort. Removes containers, network, and anonymous volumes.
5. `shutil.rmtree(handle.temp_dir, ignore_errors=True)` — removes the per-trial context directory.

A second `teardown(handle)` call finds nothing in the cache, exits quickly. Foreign handles (anything not `_LocalEnvHandle`) return silently — Protocol semantics say teardown of an already-torn-down handle is a no-op.

`close()` (run-level) walks every connected trial, closes their clients, clears the cache. Rarely necessary in practice because the conductor calls `teardown(handle)` in a `finally`; `close()` catches trials that leaked past that (e.g., a caller that forgot to teardown).

## Failure modes

| Where | What is raised | What is cleaned up before the raise |
|---|---|---|
| `provision` — no manifest | `ProvisionError(stage="provision")` | Nothing to clean |
| `provision` — compose start fails | `ProvisionError(stage="provision")` wrapping the compose error | Per-trial temp dir removed |
| `provision` — runner-client construction (host/port unresolvable) | `ProvisionError(stage="provision")` | Compose stack torn down + temp dir removed |
| `provision` — endpoint resolution (missing `db` service) | `ProvisionError(stage="provision")` | Compose stack torn down + temp dir removed |
| `await_ready` | Never raises today (`--wait` gates during provision); reserved for future backends | — |
| `endpoints` — foreign handle | `TypeError` | — |
| `endpoints` — everything else | Never raises (pure read on the handle's snapshot) | — |
| Any per-trial RPC — trial not provisioned | `RuntimeError("provision() must be called before…")` | — |
| Any per-trial RPC — inside the RPC itself | Whatever `GrpcRunnerClient` raises (typically `grpc.RpcError`) | — |
| `teardown` — foreign handle | Silent no-op (idempotency contract) | — |
| `teardown` — compose down fails | Silent, logged | Whatever succeeded before the failure |

The provisioning contract (ADR-0010) requires provisioners to make a
best-effort teardown of anything partially materialised before raising.
`PerTrialRuntimeBackend` honours that at every failure point above — no
half-provisioned resources leaked to the daemon.

## `SharedStackRuntimeBackend` vs `PerTrialRuntimeBackend` side-by-side

| Concern | `SharedStackRuntimeBackend` | `PerTrialRuntimeBackend` |
|---|---|---|
| Compose scope | One project per **run** | One project per **trial** |
| Container lifetime | Whole run | Bracketed by `provision` / `teardown` |
| Cross-trial state | Shared DB, shared runner state | Isolated per trial |
| Concurrency ceiling | One trial per stateful service | Bounded by host resources (memory, CPU, docker daemon throughput) |
| Runner client | One `GrpcRunnerClient` for the whole run | One `GrpcRunnerClient` per trial, keyed by `trial_id` |
| Client connect timing | Eager, at `connect()` | Lazy, at first RPC call |
| Network isolation | Trial ids in URL paths | Docker network per trial (no cross-project reachability) |
| Volume isolation | None (shared) | Docker anonymous volumes per trial; removed on `teardown` |
| Backwards compat | Yes — default backend | Yes — opt-in via task's `environment_manifest` |
| Config gate | Always available | Task declares an `environment_manifest`; orchestrator selects the backend based on config |

Both satisfy the same `RuntimeBackend` Protocol. Callers depend only on the Protocol — swapping backends is a construction-time choice, not a callsite change.

## What this PR does *not* do

- **No Conductor / Orchestrator wiring.** `Conductor.run()` still uses the pre-ADR-0013 call sites for the shared-stack path; nothing in this PR changes those. Wiring the conductor to call `provision(spec)` / `endpoints(handle)` / `teardown(handle)` around every trial is the next PR (Phase 3.D).
- **No runner image publication.** The `tolokaforge/runner` image is still a local build. The integration test in this PR uses a public-images-only fixture (`postgres:16` + `nginx:alpine`) so the lifecycle can be exercised without our runner image; real RPC coverage waits for a follow-up that publishes the runner image or builds it in CI.
- **No opt-in from any real task pack.** Zero task packs declare an `environment_manifest` today; the validation-gate PR (Phase 3.E) will migrate one existing multi-service task to prove the design end-to-end. On that PR's green, ADR-0009 and ADR-0010 flip from `Proposed` to `Accepted`.
- **No endpoint customisation.** The `runner_service` / `db` service / `rag` service conventions are hardcoded here. `runner_port` / `db_service` / `db_port` / `rag_service` / `rag_port` manifest fields are a follow-up ticket.
- **No perf optimisations.** Image pre-pull, postgres template-DB, container pool, orphan sweep, resource caps, benchmark harness — all filed as a follow-up umbrella ticket.
- **No layered-image guide.** The SWE-bench 3-tier pattern (base → environment → instance, cited in ADR-0009) applies transparently to any pinned images the compose file references, but the concrete Dockerfile recipes for task-pack authors are a docs follow-up.

## Where to read next

- `tolokaforge/core/per_trial_runtime.py` — implementation.
- `tolokaforge/core/runtime.py` — the `RuntimeBackend` Protocol + `InMemoryRuntimeBackend`.
- `tolokaforge/core/shared_stack_runtime.py` — `SharedStackRuntimeBackend` + `RunnerClient` Protocol + `GrpcRunnerClient`.
- `tests/canonical/test_per_trial_runtime_backend.py` — the unit tests exercise every lifecycle branch documented above with fakes.
- `tests/integration/docker/test_per_trial_runtime_backend_integration.py` — the real-daemon lifecycle smoke.
- `docs/architecture/adr/0010-runtime-backend-provisioning-contract.md` — the contract this document implements.
