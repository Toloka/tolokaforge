# 0039. Standalone-Grader Substrate — Multi-Topology Grading Behind One Protocol

- **Status:** Proposed
- **Date:** 2026-08-24
- **Deciders:** @CiroGamboa
- **Consulted:** Harbor framework, Inspect AI (UK AISI), Braintrust, SWE-bench, METR
- **Milestone:** [#36](https://github.com/Toloka/tolokaforge/milestone/36) (umbrella: #1259)
- **Supersedes:** none
- **Extends:** [0038 — Grader detachment](0038-grader-detachment.md)

## Context

ADR-0038 introduced the `TrialGrader` plug-in seam and the standalone grader image / gRPC service. Four graders registered under the seam (`runner_rpc`, `grader_rpc`, `queue`, `judge_only`), but the standalone service today mounts `_unwired_judge_fn` and returns `NotImplementedError` on every RPC. Only `runner_rpc` actually grades — because it lives inside the runner and reads DB / KB / filesystem / state-diff directly from live process state.

Making the standalone service grade the full surface (state_checks + transcript_rules + trace_checks + custom_checks + llm_judge) requires the grader to *see* those inputs. There are five ways to deliver them, each a real deployment shape used in the wider LLM-eval ecosystem:

- **In-process** — grader and runner in one Python process. Aggregate image; tolokaforge's current `runner_rpc`. Simplest; no wire hop.
- **Live callback** — grader in its own process/container dials the runner's read-only state RPC on demand. Inspect AI's pattern; small wire per call; grader depends on runner being alive.
- **Trajectory-storage callback** — grader dials a separate storage service (in development inside Toloka) that holds traces + environments + per-trial state. Same shape as live callback; different source.
- **Snapshot-on-wire** — the runner packages all state the grader needs into the RPC. Harbor's `[verifier.environment_mode = "separate"]` + `[[verifier.collect]]` hooks. Fully independent grader; expensive wire for large workspaces.
- **Shared-mount** — grader in a sidecar container reading a filesystem/DB mount shared with the runner. SWE-bench / METR's pattern. Cheap; host-locality constraint.

Beyond the topology axis, sub-components inside grading (rubric evaluator, judge model provider, transcript-rule matcher, trace-check operator, state-check backend, custom-check executor) are hard-coded in runner-side code today. An operator wanting a custom judge or a custom rubric evaluator must fork the framework.

Tolokaforge needs the grader to work under (1), (2), and (3) *this milestone* — the aggregate image, the independent container image, and the future trajectory-storage service must all be viable targets. It also needs (4) and (5) available as documented future modes without implementation cost now: the wire-size implications of snapshot-on-wire are real (coding tasks can have ~100 MB workspaces), and shared-mount only makes sense for single-host deployments — neither is worth the risk of a full ship this milestone.

## Decision

**Introduce a `GradingSubstrate` Protocol** as the single abstraction over "how does the grader see the trial's state?" Every deployment topology is an implementation of the same Protocol. The component evaluator code above the substrate never changes.

**Ship two implementations this milestone:**

- `InProcessGradingSubstrate` — wraps runner-side objects directly. Powers the aggregate image and the current `runner_rpc` path (unified under one code path).
- `LiveRunnerCallbackGradingSubstrate` — dials a new read-only substrate gRPC service on the runner. Powers the independent grader container.

**Reserve three future implementations** as declared Protocol-implementing stubs with recipes carried in this ADR:

- `TrajectoryStorageGradingSubstrate` — dials the (in-development) trajectory-storage service.
- `SnapshotGradingSubstrate` (Harbor pattern) — state travels inside `GradeRequest`.
- `SharedMountGradingSubstrate` (SWE-bench pattern) — reads from a shared mount.

**Register substrate implementations under a plug-in group** — `tolokaforge.grading_substrates`. Ships with two entries this milestone; when trajectory-storage lands, it registers itself as one entry-point line — no framework PR.

**Introduce six new entry-point groups for sub-component evaluators** (rubric_evaluators, judge_model_providers, transcript_rule_matchers, trace_check_operators, state_check_backends, custom_check_executors). Every existing implementation becomes the reference impl behind its Protocol — extract-refactor, zero behaviour change.

**The composite dispatch** (`GraderServiceImpl.Grade`) resolves the substrate from `RunConfig.grader.substrate`, constructs it, then runs each selected component evaluator over it. `runner_rpc` uses the same dispatch with `InProcessGradingSubstrate`; the code path is unified.

**Backward compatibility is a first-class deliverable.** `runner_rpc` behaviour is preserved from the operator's view. Every existing `grading.yaml` runs untouched. No task auto-migrates to `grader_rpc`.

## Consequences

**Positive**

- Three deployment topologies operational this milestone (aggregate image, independent grader container, unified `runner_rpc` path).
- The trajectory-storage service, when it ships, registers itself with a one-line entry point.
- Snapshot mode and shared-mount mode become documented, additive future shifts — the Protocol is already shaped for them.
- Component evaluators become individually pluggable; operators can extend judges / rules / trace ops without touching the framework.
- Extract-refactor of runner-side grading code onto the Protocol path unifies the codebase and locks behaviour parity by construction.
- **Phase 3 landing (issue #1263).** `GraderCompositeDispatch` (`tolokaforge/grader/composite_dispatch.py`) is mounted by `python -m tolokaforge.grader`: it deserialises the wire v2 fields, builds `LiveRunnerCallbackGradingSubstrate` per trial, runs the composite grading pipeline, and returns a real `Grade` on the `grader_rpc` path.
- **Phase 4 parity gate (issue #1264).** `tests/canonical/test_grader_parity_reference.py` operationalizes the multi-substrate parity claim across the six sub-component seams; hash grading refusal and KB passthrough divergence are recorded per-pack on `parity.yaml`.

**Negative**

- Adds a new gRPC surface (`SubstrateService`) on the runner. Config-gated (`RunConfig.grader.expose_substrate: false` default) — no impact on runs that don't need it, but it's a new component with its own maintenance cost.
- `LiveRunnerCallbackGradingSubstrate` ties grader lifecycle to runner lifecycle (grader fails if the runner is torn down mid-grade). Documented; caller sees `GradingFailedError`. Snapshot mode later decouples this.

**Neutral**

- One more Protocol in the codebase. Familiar shape (identical to `TrialGrader`); low cognitive cost.

## Reserved future substrate — `TrajectoryStorageGradingSubstrate`

**When to ship:** trajectory-storage service is live and stable. Grader wants to grade completed trials whose runners have been torn down (bulk rescoring, cross-run analysis).

**Wiring recipe:**

1. Add `SUBSTRATE_MODE_TRAJECTORY_STORAGE` to the `SubstrateMode` enum in `grader.proto`.
2. Implement `TrajectoryStorageGradingSubstrate(client: TrajectoryStorageClient, trial_id: str)`. Its methods delegate to storage-service RPCs: `client.get_trial_db(trial_id) -> DBSnapshot`, `client.read_trial_file(trial_id, path) -> bytes`, etc.
3. Register under `tolokaforge.grading_substrates` entry point: `trajectory_storage = tolokaforge.core.grading.substrate:TrajectoryStorageGradingSubstrate`.
4. Extend `RunConfig.grader.trajectory_storage_address` to carry the service URL when the operator selects `substrate: trajectory_storage`.
5. Ship as a separate PR against `main`, coordinated with the trajectory-storage team.

**No changes to evaluator code, `runner_rpc`, or the aggregate image path.**

## Reserved future substrate — `SnapshotGradingSubstrate` (Harbor pattern)

**When to ship:** offline replay / cross-region grading becomes a hard requirement (grader outlives the runner, or lives in a different network region).

**Wiring recipe:**

1. Extend `grader.proto` `GradeRequest` v3 with snapshot fields:
   - `initial_state_json`, `final_state_json` — DB snapshots at trial start / end.
   - `filesystem_snapshot: bytes` — tar of agent-visible files (already filtered by the runner's `_read_agent_visible_filesystem` — never `node_modules`, `.venv`, `.git`).
   - `checks_module_bytes: bytes` — `checks.py` bytes when `custom_checks` is enabled.
   - `id_fields`, `unstable_fields`, `judge_model_config` — authored inputs.
   - `kb_snapshot` — for tasks with deterministic KB corpora, the vector-store manifest. Live-TypeSense tasks stay on live-callback.
2. Implement `SnapshotGradingSubstrate` unpacking the fields into the same shape `LiveRunnerCallbackGradingSubstrate` produces.
3. Register under `tolokaforge.grading_substrates`.
4. **Filesystem cap policy** — 32 MB soft cap. Tasks exceeding the cap auto-fall-back to `LiveRunnerCallbackGradingSubstrate` (documented behaviour; not a bug). Config: `RunConfig.grader.fallback_on_snapshot_error: live_callback` (default).
5. Snapshot builder in the client (`GraderRPCTrialGrader.grade`) reads the filesystem into a tar and skips the wire path when the cap is exceeded.

**Why this is riskier than live-callback:** coding tasks with ~100 MB workspaces would need the auto-fallback path; a bug in the size check could break existing pipelines. Shipping later when the platform has learned the wire behaviours.

## Reserved future substrate — `SharedMountGradingSubstrate` (SWE-bench pattern)

**When to ship:** a single-host high-throughput deployment wants a separate grader container but doesn't want the wire hop.

**Wiring recipe:**

1. Add `SUBSTRATE_MODE_SHARED_MOUNT` to the enum.
2. Implement `SharedMountGradingSubstrate(mount_root: Path, trial_id: str)`. Reads from a shared filesystem/DB mount populated by the runner.
3. Register under `tolokaforge.grading_substrates`.
4. Extend the standalone compose recipe with `volumes:` shared between runner + grader.
5. **Constraint:** grader and runner must be on the same host. Documented.

## References

- [Harbor `verifier.environment_mode = "separate"`](https://www.harborframework.com/docs/tasks)
- [Inspect AI scorer callback + deferred scoring](https://inspect.aisi.org.uk/scorers.html)
- [Braintrust sandboxed scorer](https://www.braintrust.dev/docs/platform/functions/scorers)
- [SWE-bench harness](https://www.swebench.com/SWE-bench/reference/harness/)
- [METR task standard](https://github.com/METR/task-standard)
