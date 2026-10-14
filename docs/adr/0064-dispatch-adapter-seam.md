# 0064. The dispatch adapter seam

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Superseded by:** none

## Context and Problem Statement

A whole trial is already a self-contained job. `TrialNodeInputs` plus
`run_trial_from_inputs` ([ADR-0057](0057-node-runnable-trial-unit.md)) run it
without the orchestrator, and `run_trial_node(inputs) -> TrialResult`
([ADR-0063](0063-whole-trial-node-service-and-dispatch-seam.md), today the
`TrialOwningBackend` Protocol in `core/trial_executor.py`) is the point where the
control plane hands that job to somewhere else. Both are already shaped like an
adapter contract.

They are packaged the wrong way. `remote` is registered as a peer of the
substrate backends (`shared`, `per_trial`, `in_memory`) in the single
`tolokaforge.runtime_backends` group, so two different questions share one
axis: *where a job runs* (dispatch) and *how its sandbox is built* (substrate).
The tangle has visible costs: `remote` carries stub per-trial methods that raise,
its node placement is a static registry in config, its exceptions are named after
nodes, and selecting it goes through the deprecated `orchestrator.runtime` knob.
The legacy `worker` plus queue path, many full orchestrators pulling from a
queue, is the opposite failure: it replicates the whole control plane instead of
dispatching jobs.

## Decision Drivers

- **One orchestrator, many places to run.** The orchestrator stays a lean,
  centralized job scheduler; execution, scaling and durability belong to the
  backend that runs the job.
- **Backend-agnostic.** Open-source users must be able to adapt tolokaforge to
  their own runtime, so no single execution system is wired into core.
- **Two axes, kept apart.** Dispatch and substrate vary independently.
- **Testable without infrastructure.** Distributed dispatch must be exercisable
  on one machine with no external service.
- **Single-box unchanged.** The default path must stay as it is.
- **One surface across harnesses.** Native, Harbor and Inspect runs executed
  through tolokaforge share one trace, grading, report and dispatch surface.

## Considered Options

1. **Keep `remote` as a runtime-backend peer.** Rejected: the axes stay tangled
   and the node-fleet shape stays baked in, which does not fit a durable
   workflow engine that owns placement itself.
2. **Hard-wire one workflow engine (Temporal) into core.** Rejected: core would
   depend on one backend, against the open-source extensibility goal.
3. **A backend-agnostic `DispatchAdapter` axis, with Temporal as the first
   external adapter.**

## Decision

We adopt **option 3**.

**The seam.** `DispatchAdapter` is a first-class Protocol: given a job
(`TrialNodeInputs`) it returns a `TrialResult`. It has its own entry-point group,
`tolokaforge.dispatch_adapters`, and its own build context. A `DispatchAdapter`
is not a `RuntimeBackend`: it implements none of the substrate lifecycle and
`TrialOwningBackend` is replaced by it.

**A dispatch-neutral vocabulary.** Adapters raise three exceptions that name no
transport:

- `DispatchRefused` — a deterministic refusal; becomes a non-retryable
  `PROVISION_ERROR` result with a failure bundle.
- `DispatchTransient` — an infrastructure fault; raised, and retried by the
  orchestrator.
- `DispatchProvisionError` — the backend could not place or start the job.

**Selection.** A new `orchestrator.dispatch` config block names the adapter and
its options. When the block is absent the run takes the existing single-box
substrate path, unchanged. `orchestrator.runtime: remote` becomes a one-release
deprecated alias for the `grpc_node` adapter.

**Three adapters.**

- `local` is the native, zero-dependency default and the local-distributed test
  bed. It has an in-process thread mode and a mode that auto-spawns a local
  trial-node subprocess, so the dispatch path runs end to end with no external
  service.
- `grpc_node` is today's remote path recast as one adapter: lease a node, ship
  the job over `RunTrial`, release the lease.
- `temporal` is designed for and built outside core, with Toloft. A job maps to
  an activity, a job category maps to a task queue, and durability, scaling and
  cancellation come from Temporal. Tolokaforge core never imports Temporal.

The distributed backend owns execution, scaling and durability; tolokaforge does
not build a node fleet of its own.

### Open decisions

To be resolved before implementation:

- Deprecate `orchestrator.runtime: remote` as a one-release alias, or remove it.
- Where the Temporal adapter lives: a solutions repository or a plugin wheel.
- Move `grpc_max_message_mb` off the job so the job is fully transport-neutral.
- Which trial-unit parity gaps ship so the job covers every scenario (the
  refusals listed in ADR-0063).

## Consequences

- Backend-agnostic and extensible: other runtimes plug in through an entry-point
  group, in or out of tree.
- Native, Harbor and Inspect runs executed through tolokaforge share one
  observability (Langfuse), grading, report and dispatch surface.
- The `local` adapter makes distributed dispatch testable on one machine.
- Custom durability and node-fleet work is dropped in favour of the backend.
- Single-box stays the default and is unchanged.
- The `remote` runtime-backend stubs, the node-named exceptions and the
  `TrialOwningBackend` Protocol go away once the adapters land; `RemoteRuntimeBackend`'s
  lease logic moves into `grpc_node`.
- A job crosses a process boundary by value, so everything a scenario needs must
  travel in `TrialNodeInputs`; what it cannot carry stays refused.

## References

- [ADR-0057](0057-node-runnable-trial-unit.md), [ADR-0058](0058-detached-execution.md), [ADR-0059](0059-remote-runtime-backend.md), [ADR-0063](0063-whole-trial-node-service-and-dispatch-seam.md)
- [`docs/DETACHED_EXECUTION.md`](../DETACHED_EXECUTION.md)
- "Tolokaforge on Temporal" analysis (design note, outside the repository)
