# 0057. The node-runnable trial unit and its standalone entry

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Superseded by:** none

## Context and Problem Statement

A trial's composition — trial grader, conductor, provisioning executor — was
built in two places. The orchestrator built it for a batch run, and the
`run_trial` library entry rebuilt a smaller copy of it for one trial. The two
copies disagreed: the library copy never gave the trial grader the in-process
routing shim, so `runtime="per_trial"` graded against whatever
`EXECUTOR_ADDRESS` named instead of the trial's own runner; it propagated
`ProvisionError` through its own provision / run / teardown bracket while the
executor synthesised a failed result; and it ignored the run's gRPC message
bound.

The ability to run *one* trial on a machine that holds only serialized inputs
and a mounted task pack — with no orchestrator in the process — needs a single
composition, a serializable input contract, and an adapter seam that works for
delegated adapters as well as the native one.

## Decision Drivers

- **One composition.** Whatever builds a trial's grader, conductor and executor
  does so in one place, so a fix lands once ([ADR-0015](0015-trial-executor-protocol.md),
  [ADR-0014](0014-trial-grader-protocol.md)).
- **No second serialization.** The `run-trial` subprocess wire
  ([ADR-0022](0022-runtime-independence.md)) already ships a full `TaskConfig`
  as JSON. A node reconstructs the same rich config from it.
- **No orchestrator in the call graph,** and none in the import closure.
- **Credentials never travel in the inputs.** Model configs carry
  `${secret:NAME}` references; the node resolves them from its own secret
  payload.
- **Fail loud on a drifted pack.** A node's mounted pack must be the one the
  trial was planned against.

## Considered Options

1. **Keep two compositions** and patch each as defects surface.
2. **A second wire for nodes** carrying a pre-digested task description.
3. **One execution unit, one input contract that reuses the `TaskConfig`
   wire, one entry, and an optional adapter seam.**

## Decision

We adopt **option 3**.

**The unit.** `build_trial_execution_unit(TrialExecutionContext)` in
`tolokaforge.core.trial_node` composes the grader, the conductor and a
`ProvisioningTrialExecutor` from one frozen context. The orchestrator and the
standalone entry both call it. The in-process grader routing shim is derived
inside it (the runtime backend, when it exposes no static runner address), so
every entry gets it.

**The input contract.** `TrialNodeInputs` (`tolokaforge.core.trial`) is pure
data: the full `TaskConfig`, the `TrialSpec`, the run's `GraderConfig`,
`grpc_max_message_mb`, the runtime and conductor names, and the task pack's
content hash. It also names where the node mounted the pack, where to write
artifacts, and the rate-limit probe block. It names no live dependency and no
remote runner or grader address — those are not part of this contract. The
pack directory and hash are set together or not at all; a task with no file
assets sets neither.

`TaskConfig` crosses the contract exactly as it crosses the `run-trial` wire
(`TaskConfig.model_validate(...)`). Its source directory is deliberately not
serialized, so the node uses the mounted pack only for file assets and for the
content-hash check.

**The entry.** `run_trial_from_inputs(inputs) -> TrialResult`, re-exported from
`tolokaforge.runner`, verifies the pack hash, installs the process secret
manager from `TOLOKAFORGE_SECRETS_JSON` when the payload is present (the
existing container contract), rebuilds the adapter, LLM client and runtime
backend, builds the unit with `provision_error="propagate"`, and owns the
backend's `connect()` / `close()`. A node runs **one trial per process**: the
secret manager is process-global.

**The adapter seam.** `BaseAdapter.register_preloaded_task(task, task_dir)` is
optional. The default raises a "not node-runnable" error; an adapter that
supports it overrides it. The native and terminal-bench adapters do, so a
delegated adapter is node-runnable on the same terms as the native one.

**One bracket.** The library `run_trial(task=..., models=...)` — its signature
unchanged — resolves the task's wire description, packs the call into
`TrialNodeInputs`, and calls `run_trial_from_inputs`. The `run-trial`
subprocess surface calls `run_trial`, so it reaches the same entry and still
reports a provisioning failure as `error_type: "ProvisionError"`.

## Consequences

- The grader-routing defect above is fixed by construction: the library entry
  no longer composes its own grader.
- `ProvisionError` still propagates on every single-trial surface, and a batch
  run still synthesises a failed result, because the policy is a parameter of
  one executor rather than two brackets.
- The pack hash covers every regular file under the pack directory, so editing
  an unrelated file in a task directory refuses the trial. That is the intended
  strictness for a node.
- The entry's import closure excludes `tolokaforge.core.orchestrator`; a test
  loads it in a fresh interpreter to hold the line.
- Remote addressing of a runner or grader from a node is outside this decision.
