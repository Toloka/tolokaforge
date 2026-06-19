# ADR 0001 — `TrialSpec` and `TrialResult` as the typed control↔trial seam

**Status:** Accepted (introduced together with the implementing PR).

## Context

The engine has no named type for what flows from the orchestrator to a per-trial execution. Today the runner gRPC contract carries a serialised `TaskDescription` as `RegisterTrialRequest.task_description_json` and the orchestrator threads the rest of the per-trial context (`trial_id`, model config, retry-related state, runtime endpoints, …) as ad-hoc keyword arguments to internal helpers.

That works for the single-process case but leaves the control↔trial boundary informal. Every later refactor — typed runtime endpoints, a `RuntimeBackend` interface, extracting a conductor that runs trials in a different process, an at-scale backend that needs to schedule trials across machines — either has to invent its own shape or extend the existing JSON blob with new informal fields. The boundary is real but unnamed; it can drift in either direction and there is no single review surface where its shape is discussed.

A symmetrical gap exists on the return path. `TrialRunner.run()` returns a `Trajectory`; `GradeTrial` returns a `Grade` over a separate RPC; the orchestrator stitches them together inline. There is no single name for "the result of a trial."

## Decision

Introduce two Pydantic v2 models in a new module `tolokaforge/core/trial.py`:

- **`TrialSpec`** — the typed control→trial payload. Embeds the existing `TaskDescription` at `spec.task`; adds the per-trial context that is currently scattered as ad-hoc kwargs (identity: `trial_id` / `run_id` / `attempt_id` / `worker_id`; execution parameters: `agent_model_config`, `user_model_config`, `max_turns`, `default_tool_timeout_s`); reserves two forward-looking extension points (`env_endpoints: dict[str, str]`, `runtime_context: dict[str, Any]`) that later stages will type in place.
- **`TrialResult`** — the typed trial→control return shape. Deliberately thin: `Trajectory` (`tolokaforge.core.models.Trajectory`) already carries the trial's status, grade, metrics, message trace, tool log, and final environment state. `TrialResult` adds only the canonical combined trial identifier and a forward-looking `worker_id` slot; everything substantive remains on the embedded `Trajectory`. No duplicate fields.

Switch the gRPC contract:

```diff
 message RegisterTrialRequest {
   string trial_id = 1;
-  string task_description_json = 2;
+  string trial_spec_json = 2;
   double default_tool_timeout_s = 3;
 }
```

Single replacement. No deprecation window. The runner reads `spec.task` and uses the embedded `TaskDescription` for the existing downstream logic; everything past the parse boundary is unchanged.

The orchestrator builds one `TrialSpec` at the top of its per-trial helper and accesses `spec.<field>` throughout; the helper returns a `TrialResult`. Callers that need the trajectory read `result.trajectory`.

## Consequences

**Positive.**

- A single typed surface where the control↔trial boundary is discussed and reviewed. Future seams reference fields on `TrialSpec` / `TrialResult` rather than inventing parallel shapes.
- The runner gRPC payload becomes self-describing: a `TrialSpec` carries identity, task, and execution context together rather than relying on the surrounding RPC for context.
- The forward-looking extension points (`env_endpoints`, `runtime_context`) are explicit slots. Later stages can introduce typed `EnvEndpoints` and runtime-specific contexts by replacing those slots' types, without revising the surrounding `TrialSpec` shape.
- Future remote conductors (runner-in-a-different-process) receive a typed message instead of a JSON blob.

**Negative.**

- One breaking change to the runner gRPC contract: the `task_description_json` field is renamed to `trial_spec_json` and the payload shape changes. Anything that calls `RegisterTrial` directly must update in lockstep. In this repo the only producer is the orchestrator itself; external producers (private adapter packages) take the same field rename.
- The `TrialSpec` field set is a one-shot decision; anything missed now becomes an extension to the model. The forward-looking dict slots are the explicit escape hatch.

## Alternatives considered

- **Add `trial_spec_json` alongside `task_description_json` for a transition window.** Rejected: parallel-paths purgatory contradicts the project's fail-fast posture (`AGENTS.md`). The field is part of an internal contract with one (in-repo) producer; coordinating a single-PR cutover is straightforward.
- **Make `TrialSpec` a protobuf message rather than a Pydantic-to-JSON-string field carried as a `string`.** Rejected for this round: matches the existing pattern (`task_description_json` was the same shape) and keeps the proto thin. Revisit when a remote conductor actually exists and the cost of JSON-parse-per-RPC is measurable.
- **Define `TrialResult` with duplicate fields (status, grade, metrics) parallel to `Trajectory`.** Rejected: those fields are already on `Trajectory`. Duplicating them invites drift and adds no information.
- **Skip `TrialResult` entirely and rename `Trajectory` later when needed.** Rejected: a typed return for the per-trial helper is more useful than a future rename; the thin-wrapper cost is one extra type and a `from_trajectory` constructor.
