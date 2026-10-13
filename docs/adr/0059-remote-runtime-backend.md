# 0059. The remote runtime backend and node registry

- **Status:** Accepted — the backend's per-trial RPC dispatch is replaced by whole-trial dispatch in [ADR-0060](0060-whole-trial-node-service-and-dispatch-seam.md); the registry and the run-start gating stand
- **Date:** 2026-10-13
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Superseded by:** none

## Context and Problem Statement

[ADR-0058](0058-detached-execution.md) separates the orchestrator's control
work from the execution of one trial, and [ADR-0057](0057-node-runnable-trial-unit.md)
makes one trial a portable unit. What is still missing is the hand-off: an
orchestrator that sends a trial to a runner on another machine and gets a
graded result back.

Every runtime backend so far binds trials to a runner it started itself (a
local stack, or a per-trial compose project). The existing runner client
already speaks to any runner address over gRPC, so the gap is *which address a
trial gets*, not a new transport.

## Decision Drivers

- **Reuse the runner client.** The wire already carries registration, tool
  execution and grading; a second transport would split the contract.
- **Native and delegated trials dispatch identically.** Both reach the runner
  through `execute_tool` / `grade_trial`, so the backend need not tell them apart.
- **Single-box unchanged.** `--runtime shared` must behave exactly as before.
- **Bounded concurrency.** A node runs one trial at a time; asking for a node
  when none is free must wait, not fail or double-book.
- **Fail loud.** A run shape the backend cannot serve is refused at run start,
  not degraded mid-run.

## Considered Options

1. **Extend the shared backend with a remote mode.** Rejected: it carries a
   static runner address and a local-stack lifecycle that do not apply.
2. **A networked registry with node self-registration.** Rejected: it adds a
   service and an auth surface that the dispatch path does not need.
3. **A new backend leasing from a node registry seam; the first registry is
   config-seeded and in-process.**

## Decision

We adopt **option 3**.

**The registry.** `NodeRegistry` (`tolokaforge.core.node_registry`) is a
Protocol: `register_node`, `lease(trial_id)`, `release(trial_id)`, `has_nodes`.
`lease` blocks until a node is free and raises on timeout; `release` is
idempotent. `InMemoryNodeRegistry` is seeded from
`orchestrator.remote.nodes` (bare `host:port` strings, with synthesised node
ids) and holds a condition variable, so the node count bounds concurrency. Any
other registry implements the same Protocol.

**The backend.** `RemoteRuntimeBackend` (`tolokaforge.core.remote_runtime`,
registered as `remote`) leases a node in `provision`, builds a `GrpcRunnerClient`
for the leased address, connects it on the trial's first RPC, and routes every
per-trial RPC to it. `teardown` forgets the trial on the node, closes the
client and releases the lease. It exposes no `runner_address`, so the trial
grader is wired through the in-process shim to the trial's leased client.

**Run-start gating.** Under `remote` the orchestrator starts no local stack. It
refuses, before any trial: an explicit `auto_start_services: true` (a
contradiction), a task declaring an `environment_manifest` (the backend does not
materialise task-declared stacks), and the `grader_rpc` / `queue` graders (they
need a static runner address). Empty `orchestrator.remote.nodes` is refused at
config load. A grade bundle is not produced, so snapshot mode is refused by the
existing probe.

**Single-box.** The auto-start block and every default are unchanged for every
runtime other than `remote`; the new context field is `None` for them.

## Consequences

- One orchestrator can drive trials on several machines whose runners are listed
  in the run config.
- `remote` is selected through `orchestrator.runtime`, which already emits a
  `DeprecationWarning` for any explicit value; there is no non-deprecated selector.
- Node health is observed lazily: an unreachable node fails the trial that
  leased it at first RPC, under the connect budget, not at run start.
- Not supported: task-declared stacks, grade bundles, networked node
  registration, and worker-mode (multi-process) runs, which are refused because
  each process holds its own node registry.

## References

- [ADR-0057](0057-node-runnable-trial-unit.md), [ADR-0058](0058-detached-execution.md)
- [ADR-0010](0010-runtime-backend-provisioning-contract.md) — the provisioning contract the backend implements.
- [`docs/RUNTIME_BACKENDS.md`](../RUNTIME_BACKENDS.md)
