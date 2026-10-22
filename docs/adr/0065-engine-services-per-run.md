# 0065. Start only the engine services a run needs

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** @bberkes-toloka
- **Supersedes:** none
- **Superseded by:** none
- **Amends:** [ADR-0018](0018-multi-container-under-shared-runtime.md) (the built-in stacks) and [ADR-0054](0054-search-backend-protocol-and-registry.md) (the stack rule, in its Context and in § Re-homing the agent-side binding: a rag-service search starts rag-service alone)

## Context and Problem Statement

A run that declares no `environment_manifest` runs its tools on the engine's built-in services. Until now
there were two fixed sets: `core_stack` (runner + db-service) and `full_stack` (runner + db-service +
rag-service + mock-web). The orchestrator picked `full_stack` as soon as any task needed either extra
service, so the two always came together:

- a run that only needs mock-web (browser or mobile tasks, or an `http_request` that reaches mock-web)
  also built or pulled and started rag-service, a Python service that loads torch and a
  sentence-transformers model (an image of about 6.5 GB, and several hundred MB of memory once warm);
- a run that only needs rag-service (a search backend that declares it, or an adapter's
  `needs_rag_service`) also started mock-web.

The run config could not change the choice. On a single box the unused service costs once per run; where
every trial runs in its own sandbox, it costs once per trial. There, an adapter whose knowledge base is
served by TypeSense, yet declares the rag-service need, made each trial carry rag-service and mock-web and
pushed it past a 1 GiB sandbox: about 0.6 GiB of anonymous memory on the core stack, about 1.05 GiB with
the two extras and TypeSense.

## Decision Drivers

- Start what the run uses and nothing else: the cost of an unused service multiplies on per-trial substrates.
- A service the run needs is never silently missing (AGENTS.md Core Rule 1): the selection follows the
  signals the runner itself enforces.
- Run configs, the Python API and every run whose needs the engine can see behave as before.
- An operator can add a service the engine sees no need for, and can forbid one.

## Considered Options

1. **Keep the two stacks and filter with `EngineStack.start_all(profiles=...)`.** rag-service and mock-web
   already carry the `rag` and `web` profiles.
2. **Compose the stack service by service at construction time**, from a per-service decision the run
   config can override.
3. **Model the engine services as compose services in the composition plan** ([ADR-0044](0044-composition-plan-runtime.md)).

## Decision

We adopt **option 2**. Option 1 leaves `start_all` building or pulling every declared image whatever the
filter, and `RAG_SERVICE_URL` set with no rag-service running. Option 3 is a larger redesign this change
neither needs nor blocks: the per-service vocabulary below carries over to it.

- **Per-service resolution** (`resolve_engine_services` in `tolokaforge/core/engine_stack_runtime.py`):
  - rag-service when a task searches with a backend whose `stack_service` is rag-service (ADR-0054), or the
    adapter declares `DockerStackRequirements.needs_rag_service`;
  - mock-web when a task enables `browser` or `mobile`, enables `http_request` with hosts that admit
    mock-web, or declares `initial_state.mock_web`.
- **Run-config override** in the existing `docker:` block:

  ```yaml
  docker:
    services:
      rag_service: auto   # auto (default) | always | never
      mock_web: auto
  ```

  `auto` starts the service when the run needs it. `always` starts it regardless. `never` never starts it,
  and a run that needs it is refused with `EngineServiceConflictError` before any engine container work, naming
  the tasks or the adapter. The modes are `always` and `never` rather than `on` and `off`, which YAML reads
  as booleans; the config refuses a boolean and says why.
- **Composition:** `full_stack(rag_service=..., mock_web=...)` adds only the selected services, and the
  runner gets `RAG_SERVICE_URL` only with rag-service. A run that selects neither gets `core_stack`.
- **Visibility:** the run logs each decision with its reason (`Engine services resolved`).

## Consequences

### Positive

- A run that needs one extra service no longer builds, pulls or starts the other.
- The run config can add or forbid each service, and a contradiction fails before any engine container starts.
- The decision and its reasons are in the run log.

### Negative / Trade-offs

- A task pack that used the other service without any signal the engine reads no longer gets it;
  `always` restores it.
- `tolokaforge docker up --profile full` still starts both, for stacks started by hand.

### Follow-ups

- Adapters that declare `needs_rag_service` for a knowledge base another plane serves (TypeSense) should
  stop; their runs then need neither extra service.
- A caller that sizes per-trial sandboxes could read the resolved services.
- TypeSense and db-service could join the same vocabulary.

## Links

- Related ADRs: [ADR-0018](0018-multi-container-under-shared-runtime.md) (the built-in stacks),
  [ADR-0044](0044-composition-plan-runtime.md) (the composition plan),
  [ADR-0054](0054-search-backend-protocol-and-registry.md) (search backends and stack services).
- Related code: `tolokaforge/core/engine_stack_runtime.py`, `tolokaforge/docker/stacks/full.py`,
  `tolokaforge/core/models/docker_config.py`.
- Issues: #1937; #1935 (the `http_request` signal).
- Reference: [RUNNER.md § Which engine services start](../RUNNER.md#which-engine-services-start-dockerservices).
