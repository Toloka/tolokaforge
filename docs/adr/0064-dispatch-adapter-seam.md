# 0064. The dispatch adapter seam

- **Status:** Accepted
- **Date:** 2026-10-14
- **Amended:** 2026-10-15, 2026-10-21
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

- **One orchestrator, many places to run.** The orchestrator is the **unified
  control plane** — the single interface to operate the system in tolokaforge's
  own vocabulary (launch, stop, see status, collect results); execution, scaling
  and durability belong to the backend that runs the job.
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
2. **Hard-wire one specific workflow engine into core.** Rejected: core would
   depend on one backend, against the open-source extensibility goal.
3. **A backend-agnostic `DispatchAdapter` axis, with an external distributed
   backend as the first out-of-core adapter.**

## Decision

We adopt **option 3**.

**The seam.** `DispatchAdapter` is a first-class Protocol: given a job
(`TrialNodeInputs`) it returns a `TrialResult`. It has its own entry-point group,
`tolokaforge.dispatch_adapters`, and its own build context. A `DispatchAdapter`
is not a `RuntimeBackend`: it implements none of the substrate lifecycle and
`TrialOwningBackend` is replaced by it.

`run_trial_node` is the synchronous submit-and-await convenience every adapter
has. A sibling `ControllableDispatchAdapter` Protocol adds the control-plane
operations the orchestrator needs to operate a job after it is submitted —
id-keyed `cancel` / `status` / `fetch_result` over a `JobHandle`. Status reuses
the run-queue state vocabulary (`JobState`), results reuse `TrialResult`, and
collection ties to the offline bundle. The operations are a separate Protocol,
not extra methods on `DispatchAdapter`, so a third-party three-method adapter
keeps conforming unchanged. Implementation status: `local` is real for status
and pre-start cancel, plus a process kill for a subprocess worker, and
best-effort otherwise; `grpc_node` carries honest stubs — a one-RPC node exposes
no control plane; an external distributed backend implements them fully.

**A dispatch-neutral vocabulary.** Adapters raise three exceptions that name no
transport:

- `DispatchRefused` — a deterministic refusal; becomes a non-retryable
  `PROVISION_ERROR` result with a failure bundle.
- `DispatchTransient` — an infrastructure fault; raised, and retried by the
  orchestrator.
- `DispatchProvisionError` — a deterministic placement/start fault (bad compose,
  missing image, bad manifest); non-retryable. A backend with no capacity *now*
  raises `DispatchTransient` instead, so the job is retried.
- `DispatchUnsupported` — the honest "this backend can't do that" refusal for a
  control operation it cannot serve. A `DispatchRefused` subclass, so it travels
  the existing non-retryable path unchanged.

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
- an **external distributed backend** adapter is designed for and built outside
  core as a separate project or plugin wheel. A job maps to the backend's unit of
  work, a job category maps to one of its work queues, and durability, scaling,
  cancellation, status and result collection come from the backend. Tolokaforge
  core never imports it.

The distributed backend owns execution, scaling and durability; tolokaforge does
not build a node fleet of its own.

### Deferred decisions

Settled for now; revisit as the model matures:

- `orchestrator.runtime: remote` is kept as a one-release deprecated alias for
  `dispatch.adapter: grpc_node`.
- `grpc_max_message_mb` stays in adapter config; moving it off the job for full
  transport-neutrality is a follow-up.
- Where an external backend adapter lives — a separate project or a plugin
  wheel — is finalised when it is built.
- Trial-unit parity gaps (the refusals listed in ADR-0063) ship incrementally so
  the job covers every scenario.
- Commands over data sources and fleet operations stay direction-only for now.
- CLI `stop` / `status` / `results` verbs over the control plane are a follow-up.
- Wiring the run-queue `cancelled` terminal state through to the control-plane
  cancel path is a follow-up.

## Consequences

- The orchestrator is now the unified control interface: one TF-vocabulary
  surface to launch, stop, see status and collect results, over any backend.
- The control operations are an optional sibling Protocol, so a third-party
  `DispatchAdapter` that implements only the three-method contract keeps working.
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

## Amendment (2026-10-21): the library-first backend inverts the seam

Considered option 3 and the "external distributed backend" adapter block above
assume the first out-of-core backend is reached **through** the `DispatchAdapter`
seam — the orchestrator owns the run loop and hands each job to the adapter.

For the library-first target the direction **inverts**. An external orchestrator
owns the run loop and calls tolokaforge as a library via the three-step API
(`plan_trials` → `run_trial_from_inputs` → `aggregate_results`, see
[`docs/API.md`](../API.md)). Such a backend is **not** reached through the
`DispatchAdapter` seam at all: there is no tolokaforge orchestrator above it to
hold the seam. The two integration shapes stand side by side — pick by which side
owns the run loop:

- **Engine-owned run** — the tolokaforge orchestrator drives the batch and
  dispatches whole trials. This is the `DispatchAdapter` route: `local`,
  `grpc_node`, and any engine-driven external adapter.
- **Library-route run** — an external orchestrator drives the batch and calls the
  three-step API per trial. No `DispatchAdapter` is involved.

## References

- [ADR-0057](0057-node-runnable-trial-unit.md), [ADR-0058](0058-detached-execution.md), [ADR-0059](0059-remote-runtime-backend.md), [ADR-0063](0063-whole-trial-node-service-and-dispatch-seam.md)
- [`docs/DETACHED_EXECUTION.md`](../DETACHED_EXECUTION.md)
- Dispatch/runtime strategy analysis (internal design note, outside the repository)
