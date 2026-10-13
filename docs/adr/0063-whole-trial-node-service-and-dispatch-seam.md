# 0063. The whole-trial node service and dispatch seam

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** @CiroGamboa
- **Supersedes:** [ADR-0059](0059-remote-runtime-backend.md) (its per-trial RPC dispatch only)
- **Superseded by:** none

## Context and Problem Statement

[ADR-0059](0059-remote-runtime-backend.md) leases a runner node per trial and
keeps the whole trial on the control plane: the conductor and the agent's LLM
loop run in the orchestrator process and every tool call and the grading RPC
cross the network to the node's runner. That puts a network round trip inside
each agent turn, ties the trial's liveness to one long-lived chatty connection,
and leaves the node as a remote tool executor rather than a place a trial runs.

[ADR-0057](0057-node-runnable-trial-unit.md) already made one trial a portable
unit with a standalone entry, `run_trial_from_inputs`. The missing pieces are a
service that hosts that entry on a node, a wire contract to ship it inputs and
get a graded result back, and a seam in the control plane that sends a trial
there instead of running it locally.

## Decision Drivers

- **The node owns the trial.** The conductor, the agent loop, the grader and the
  trial's substrate all run on the node; the control plane ships inputs once and
  receives a graded result once.
- **Single-box unchanged.** `shared`, `per_trial`, `in_memory` and the default
  must stay byte-for-byte as they were.
- **No new surface on the runner.** The runner image is a slim subset wheel that
  deliberately excludes the orchestration closure; a node needs it.
- **Independent revisions.** The node contract must version separately from the
  runner and grader contracts.
- **Fail loud, retry right.** A deterministic refusal must not burn retries; an
  infrastructure fault must be retried.

## Considered Options

1. **Keep per-trial RPCs; add a node-side agent loop later.** Rejected: it
   leaves the chatty connection in place and splits one trial across two hosts.
2. **Grow the runner service with a whole-trial RPC.** Rejected: the runner
   ships as a subset wheel that excludes the conductor and orchestration code a
   trial needs, and its contract revs on tool/grade semantics.
3. **A dedicated trial-node service, selected by a backend capability.**

## Decision

We adopt **option 3**.

**The service.** `tolokaforge/trial_node/` is a sibling of `runner/` and
`grader/` with its own proto (`trial_node.proto`, package
`tolokaforge.trial_node`), generated stubs, `service.py`, `client.py`,
`protocol.py` and `__main__.py`, and a `tolokaforge trial-node` command. Two
RPCs: `RunTrial` (a single unary call that lasts as long as the trial) and
`HealthCheck`. `RunTrial` carries `TrialNodeInputs` as JSON — model configs hold
`${secret:NAME}` references only, never secret bytes — and answers with a
structured response: the `TrialResult` JSON on success, otherwise an
`error_type`, `stage` and `reason`. The engine module
`tolokaforge/core/trial_node.py` keeps its name; the new package is the service
that hosts its `run_trial_from_inputs`.

**One trial per process.** The process secret manager is global, so the node
takes a non-blocking lock around each `RunTrial` and answers a concurrent call
`NodeBusy`. Before any substrate is provisioned the node resolves every
`${secret:NAME}` reference in the inputs against its own secret payload and
refuses with `SecretPreflight`, naming what is missing. The node supplies its own
filesystem placement: artifacts under its output root, and task packs from its
pack root when one is configured, still verified against the content hash the
inputs carry.

**The version gate.** `NODE_PROTOCOL_VERSION` (1) is independent of the runner's
engine protocol version. The request declares the caller's value and the node
refuses a caller below its own; the response and `HealthCheck` report the node's
value and the client refuses a node below the engine's. A node that predates the
service answers `UNIMPLEMENTED`, which the client reports as a version skew
naming the image upgrade. The client's connect also checks that the node's
message bound is at least the engine's.

**The seam.** `TrialOwningBackend` is a `runtime_checkable` Protocol with one
method, `run_trial_node(inputs) -> TrialResult`. `build_trial_execution_unit`
selects `RemoteTrialExecutor` when — and only when — the runtime backend
satisfies it; every other backend keeps `ProvisioningTrialExecutor` verbatim.
`TrialExecutionUnit` holds a `TrialExecutor`. The grader and conductor are still
built for the unit and never invoked for a remote trial.

**The remote backend.** `RemoteRuntimeBackend` implements `run_trial_node`: lease
a node, connect within the connect budget, ship the inputs, release the lease.
Its per-trial RPC methods (`register_trial`, `execute_tool`, `grade_trial`,
`get_state`, `reset_trial`) are retired — they raise `NotImplementedError`
pointing at `run_trial_node` — and `provision` / `teardown` remain the lease
plumbing. There is no second runtime name: `--runtime remote` now means whole-trial
dispatch.

**Failure mapping.** A node `ProvisionError` and any deterministic refusal
(`SecretPreflight`, `TaskPackDrift`, `NotImplemented`, `InvalidConfig`,
`VersionSkew`, message-size skew) become a `PROVISION_ERROR` result with a
failure bundle, which the orchestrator does not retry. `NodeBusy`, `Internal` and
an unreachable node raise, and the orchestrator retries any exception a trial
raises.

**The reduced bundle.** The control plane writes `trajectory.yaml`,
`tool_log.yaml`, `metrics.yaml` and `grade.yaml` from the returned trajectory; the
node keeps its own fuller bundle under its output root.

**Run-start gating.** On top of ADR-0059's refusals, a remote run refuses what a
node does not carry: a non-native adapter or coding-harness mode, task-declared
stacks, a custom conductor factory, and every run-level setting the node
rebuilds from defaults (timeouts, turn cap, stuck heuristics, agent loop, log
capture, request throttle, fallback chain, the TypeSense plane). A configured
trace exporter warns rather than refuses.

**The image.** `trial_node.Dockerfile` installs the full base wheel and the
Docker CLI. It is not the runner subset wheel, and the runner subset partition is
unchanged: nothing under `tolokaforge/trial_node/` joins it.

## Consequences

- A remote trial costs one request and one response regardless of its turn count;
  a node dying mid-trial surfaces through gRPC keepalive as an unavailable node.
- The node's base engine images (`tolokaforge-runner:local`,
  `tolokaforge-db-service:local`) must exist on its Docker daemon; the node builds
  none.
- `RunTrial` is one unary call: no streaming progress and no mid-trial
  cancellation. A cancelled run cannot stop a trial already on a node.
- A remote trial emits no live traces; exporters see the reduced bundle.
- Delegated adapters, harness mode, task-declared stacks and the run-level
  settings above are refused because the inputs do not carry them.

## References

- [ADR-0057](0057-node-runnable-trial-unit.md), [ADR-0058](0058-detached-execution.md), [ADR-0059](0059-remote-runtime-backend.md)
- [`docs/TRIAL_NODE_SERVICE.md`](../TRIAL_NODE_SERVICE.md), [`docs/RUNTIME_BACKENDS.md`](../RUNTIME_BACKENDS.md), [`docs/GRPC_PROTOCOL.md`](../GRPC_PROTOCOL.md)
