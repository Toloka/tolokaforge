# 0003. TrialSpec and TrialResult as the typed control↔trial seam

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine has no named type for what flows from the orchestrator to a per-trial execution. Today the runner gRPC contract carries a serialised `TaskDescription` as `RegisterTrialRequest.task_description_json`, and the orchestrator threads the rest of the per-trial context (`trial_id`, `run_id`, retry attempt, model config, runtime endpoints, …) as ad-hoc keyword arguments to internal helpers.

That works for the single-process case but leaves the control↔trial boundary informal. Every later refactor — a typed `EnvEndpoints` model, a `RuntimeBackend` interface, extracting the per-trial agent loop into a swappable Conductor, eventually running that Conductor in a different process — either has to invent its own shape or extend the existing JSON blob with new informal fields. The boundary is real but unnamed; it can drift in either direction and there is no single review surface where its shape is discussed.

A symmetrical gap exists on the return path. `TrialRunner.run()` returns a `Trajectory`; `GradeTrial` returns a `Grade` over a separate RPC; the orchestrator stitches them together inline. There is no single name for "the result of a trial."

## Decision Drivers

- **Boundary fix:** the architectural arc this repo is on (split the orchestrator's responsibilities) requires a named control↔trial wire format. Every subsequent seam (env endpoints, runtime backend, conductor protocol, remote runner) references it.
- **One review surface:** the per-trial shape should evolve by reviewing a single type, not by spotting ad-hoc kwargs threaded through internal helpers.
- **Fail-fast:** per `AGENTS.md`, the engine doesn't ship parallel old + new code paths or silent fallbacks. The wire format must be enforced strictly on both ends.
- **No premature typing:** later stages will add typed `EnvEndpoints` and runtime-specific payloads. Those should slot into the spec without revising it. A small number of forward-looking dict slots is acceptable; an explosion of half-typed fields is not.
- **Minimal disruption:** the existing `Trajectory` model already carries status / grade / metrics / messages / tool log / final env state. A new return type that duplicates those fields invites drift and adds no information.

## Considered Options

1. **Two new Pydantic models — `TrialSpec` (inbound) + `TrialResult` (outbound, thin wrapper around `Trajectory`) — and switch the gRPC contract to carry a serialised `TrialSpec`.** Single-field replacement on `RegisterTrialRequest`; orchestrator builds one spec per trial; runner reads `spec.task` and validates the full spec on its side.
2. **Add `trial_spec_json` alongside `task_description_json` for a transition window.** Both fields accepted by the runner; producers migrate incrementally.
3. **Promote `TrialSpec` to a proper protobuf message rather than a Pydantic-to-JSON-string field carried as a `string`.** Strongly-typed at the proto layer; protobuf-generated accessors on both ends.
4. **Define `TrialResult` with duplicate fields (status, grade, metrics) parallel to `Trajectory`.** A self-contained result model independent of `Trajectory`'s evolution.
5. **Skip `TrialResult` entirely and rename `Trajectory` later when needed.** Carry only the inbound contract change; defer the outbound name.

## Decision

We will adopt **Option 1**.

Two Pydantic v2 models live in `tolokaforge/core/trial.py`:

- **`TrialSpec`** — the typed control→trial payload. Embeds the existing `TaskDescription` at `spec.task`; adds the per-trial context that was previously scattered as ad-hoc kwargs (identity: `trial_id`, `run_id`, `attempt_id`, `worker_id`; execution parameters: `agent_model_config`, `user_model_config`, `max_turns`, `default_tool_timeout_s`); reserves two forward-looking extension points (`env_endpoints: dict[str, str]`, `runtime_context: dict[str, Any]`) that later stages type in place without revising the surrounding shape. `extra="forbid"` keeps the wire strict.
- **`TrialResult`** — the typed trial→control return shape. Deliberately thin: `Trajectory` (`tolokaforge.core.models.Trajectory`) already carries the trial's status, grade, metrics, message trace, tool log, and final environment state. `TrialResult` adds only the canonical combined trial identifier and a forward-looking `worker_id` slot; everything substantive remains on the embedded `Trajectory`. No duplicate fields.

The gRPC contract switches in a single replacement:

```diff
 message RegisterTrialRequest {
   string trial_id = 1;
-  string task_description_json = 2;
+  string trial_spec_json = 2;
   double default_tool_timeout_s = 3;
 }
```

The runner-side handler validates the full `TrialSpec` with `TrialSpec.model_validate_json(...)` — producer-side strictness is matched by consumer-side strictness — then reads `spec.task` for the existing downstream tool-reconstruction path. Everything past the parse boundary is unchanged.

The orchestrator builds one `TrialSpec` at the top of its per-trial helper and accesses `spec.<field>` throughout; the helper returns a `TrialResult`. Callers that need the trajectory read `result.trajectory`. `attempt_id` flows from `lease.retry_count`; `worker_id` flows from the existing `lease_owner` identifier (`"orchestrator:<pid>"` in single-process mode, `"worker:<host>:<pid>"` in distributed mode).

## Consequences

### Positive

- A single typed surface where the control↔trial boundary is discussed and reviewed. Future seams reference fields on `TrialSpec` / `TrialResult` rather than inventing parallel shapes.
- The runner gRPC payload becomes self-describing: a `TrialSpec` carries identity, task, and execution context together rather than relying on the surrounding RPC for context.
- The forward-looking extension points (`env_endpoints`, `runtime_context`) are explicit slots. Later stages introduce typed `EnvEndpoints` and runtime-specific contexts by replacing those slots' types, without revising the surrounding `TrialSpec` shape.
- Symmetric `extra="forbid"` validation on both ends — drift between producer and consumer surfaces immediately, with a Pydantic `ValidationError` naming the exact failing field.
- A future remote Conductor receives a typed message instead of a JSON blob.

### Negative / Trade-offs

- One breaking change to the runner gRPC contract: the `task_description_json` field is renamed to `trial_spec_json` and the payload shape changes. Anything that calls `RegisterTrial` directly must update in lockstep. In this repo the only producer is the orchestrator itself; external producers (separate adapter packages) take the same single-field rename.
- The `TrialSpec` field set is a one-shot decision; anything missed now becomes an extension to the model. The forward-looking dict slots are the explicit escape hatch.
- JSON-as-string on the gRPC wire matches the existing pattern but defers the type-safety win of a proper protobuf message. Revisit when a remote Conductor actually exists and the cost of JSON-parse-per-RPC is measurable.

### Follow-ups

- **Typed `EnvEndpoints` model.** Replace the `dict[str, str]` annotation on `TrialSpec.env_endpoints` with a typed model and remove the hardcoded localhost / port defaults that currently flow through `Orchestrator._run_trial`.
- **`RuntimeBackend` Protocol.** Read from `spec.runtime_context` and `spec.env_endpoints`; ships with a `LocalRuntimeBackend` shim wrapping today's docker path.
- **`Conductor` Protocol.** Promotes `TrialRunner` to the default implementation of `Conductor.run(spec) → TrialResult`. The signature is already named; the existing `TrialRunner.run()` adapter contract is unchanged in this ADR.
- **Process-split remote Conductor.** Becomes a transport choice — the wire format is already `TrialSpec` over JSON-in-gRPC.

## Rejected alternatives

- **Option 2 — parallel `task_description_json` and `trial_spec_json`.** Parallel-paths purgatory contradicts the project's fail-fast posture. The field is part of an internal contract with one in-repo producer; coordinating a single-PR cutover is straightforward.
- **Option 3 — promote `TrialSpec` to a protobuf message.** Matches the existing pattern (`task_description_json` was the same shape) but adds proto schema-evolution complexity without a current consumer that benefits. Revisit at remote-Conductor time.
- **Option 4 — `TrialResult` with duplicate fields.** Those fields are already on `Trajectory`. Duplicating them invites drift and adds no information. The thin wrapper carries only what `Trajectory` lacks (a canonical combined identifier and a forward-looking `worker_id`).
- **Option 5 — skip `TrialResult`.** A typed return for the per-trial helper is more useful than a future rename; the thin-wrapper cost is one extra type and a `from_trajectory` constructor.
