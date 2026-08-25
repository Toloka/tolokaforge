# 0039. Standalone-Grader Substrate — Multi-Topology Grading Behind One Protocol

- **Status:** Accepted
- **Date:** 2026-08-24
- **Accepted-on:** 2026-08-25
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

**The composite dispatch** runs the same component evaluator code over whichever substrate the deployment topology hands it. `GraderCompositeDispatch` (grader-side, standalone image) is hard-wired to `LiveRunnerCallbackGradingSubstrate` — it dials the runner's `SubstrateService` per trial. The in-runner composite dispatch behind `runner_rpc` (runner-side) is hard-wired to `InProcessGradingSubstrate` — it wraps the runner's live objects directly. Selection between the two topologies is by grader-name: `grader.name: runner_rpc` vs `grader.name: grader_rpc` on `RunConfig.grader`, combined with `expose_substrate: true` on the runner so the standalone grader has a substrate surface to dial. The `GradingSubstrate` Protocol is what keeps the evaluator code above the substrate identical across both paths; there is no runtime substrate-selector field.

**Backward compatibility is a first-class deliverable.** `runner_rpc` behaviour is preserved from the operator's view. Every existing `grading.yaml` runs untouched. No task auto-migrates to `grader_rpc`.

## Non-goals and rationale — why the shipped grader is not fully independent

The deployed grader image dials back to the runner over `SubstrateService` gRPC (`LiveRunnerCallbackGradingSubstrate`). A grader that reads only what crosses `GradeRequest` — no callback, no runner dependency at grade time — is what most operators mean by "fully independent grader." **That's `SnapshotGradingSubstrate` (Harbor pattern), and it is deliberately deferred to a future milestone.** The reasoning:

- **Wire size for the target workloads.** Snapshot-on-wire packs the trial's initial + final DB state, filesystem tree, KB corpus, and `checks.py` bytes into one `GradeRequest`. For a state-routed task (a few tables, a handful of tool calls) the payload is small — under 100 KB. For a coding-task workspace (terminal-bench, SWE-bench-adjacent tasks) the filesystem tree alone is ~100 MB after the shipped `_read_agent_visible_filesystem` filter (which excludes symlinks and non-UTF-8 files but does NOT prune `node_modules` / `.venv` / `.git` — verified in `runner/service.py:1691-1716`). A default-behaviour grader that ships every trial's full workspace over one gRPC message would make each grade RPC a multi-hundred-megabyte transfer under a payload limit designed for control-plane messages.
- **The alternative today is cheap and correct.** `LiveRunnerCallbackGradingSubstrate` does one RPC per read — `db_reader().get_state()` is one round trip, each `filesystem_root().read_file(path)` is one round trip. Small tasks pay for what they use; large tasks materialise the filesystem tree once eagerly (documented in `docs/GRADER_SERVICE.md § Wire cost per grade component`) and stay under the wire budget by construction because the payload never crosses in a single frame.
- **Independence is available where it's cheap.** Operators who need a wire-independent grader today have two paths that ship in this milestone: (a) `runner_rpc` with `InProcessGradingSubstrate` — the grader and runner are one process, no wire at all, no independence needed; (b) `grader_rpc` with `LiveRunnerCallbackGradingSubstrate` — the grader is a separate container that dials the runner for reads. Neither is "the grader takes a snapshot and walks away," but both are correct and shipped.
- **The Protocol is shaped for the third path.** When snapshot-on-wire becomes required (offline replay, cross-region grading, a task family whose workspace is small enough to ship whole), `SnapshotGradingSubstrate` registers as one entry-point line under `tolokaforge.grading_substrates`. `grader.proto` extends with the additive fields it needs (v3). The composite dispatch above the substrate does not change. This ADR carries a concrete wiring recipe below.

**Operator-facing knob today.** Choose `grader.name: runner_rpc` for coding-task workloads with large workspaces — the docs (`docs/GRADER_SERVICE.md § Wire cost per grade component`) make this explicit and name the crossover empirically. `grader.name: grader_rpc` is correct and shipping for the deployment shape where the grader lives separately from the runner and the workspace stays small.

**Two other deliberate non-goals of this milestone:**

- **Hash grading on `grader_rpc`.** State-mutation semantics (snapshot → reset → replay golden actions → snapshot → restore) cannot ride a read-only substrate. `grader_rpc` refuses hash-enabled tasks with an actionable branch pointing at `runner_rpc`. Fixing this would require a write surface on the substrate — outside the scope of the read-only contract this milestone ships.
- **`search_policy` KB passthrough on `grader_rpc`.** The judge's KB-search tool is coupled to `mcp_core` / TypeSense infrastructure. Reconstructing that grader-side would mean pulling `mcp_core` into the grader image. Documented divergence; filed as ADR-0039 follow-up #1274.

## Consequences

**Positive**

- Three deployment topologies operational this milestone (aggregate image, independent grader container, unified `runner_rpc` path).
- The trajectory-storage service, when it ships, registers itself with a one-line entry point.
- Snapshot mode and shared-mount mode become documented, additive future shifts — the Protocol is already shaped for them.
- Component evaluators become individually pluggable; operators can extend judges / rules / trace ops without touching the framework.
- Extract-refactor of runner-side grading code onto the Protocol path unifies the codebase and locks behaviour parity by construction.
- **Phase 1 landing (issue #1261).** `GradingSubstrate` Protocol + `InProcessGradingSubstrate` + `LiveRunnerCallbackGradingSubstrate` shipped; `SubstrateService` gRPC (7 read-only RPCs, gated by `RunConfig.grader.expose_substrate: false`); five composite grading functions extracted to `tolokaforge.core.grading.composite`.
- **Phase 2 landing (issue #1262).** Six sub-component plug-in seams shipped: `custom_check_executors`, `judge_model_providers`, `rubric_evaluators`, `transcript_rule_matchers`, `trace_check_operators`, `state_check_backends`. `.importlinter` `composite-sub-component-seams` contract locks the negative-space.
- **Phase 3 landing (issue #1263).** `GraderCompositeDispatch` (`tolokaforge/grader/composite_dispatch.py`) is mounted by `python -m tolokaforge.grader`: it deserialises the wire v2 fields, builds `LiveRunnerCallbackGradingSubstrate` per trial, runs the composite grading pipeline, and returns a real `Grade` on the `grader_rpc` path.
- **Phase 4 parity gate (issue #1264).** `tests/canonical/test_grader_parity_reference.py` operationalizes the multi-substrate parity claim across the six sub-component seams; hash grading refusal and KB passthrough divergence are recorded per-pack on `parity.yaml`.

**Negative**

- Adds a new gRPC surface (`SubstrateService`) on the runner. Config-gated (`RunConfig.grader.expose_substrate: false` default) — no impact on runs that don't need it, but it's a new component with its own maintenance cost.
- `LiveRunnerCallbackGradingSubstrate` ties grader lifecycle to runner lifecycle (grader fails if the runner is torn down mid-grade). Documented; caller sees `GradingFailedError`. Snapshot mode later decouples this.

**Neutral**

- One more Protocol in the codebase. Familiar shape (identical to `TrialGrader`); low cognitive cost.

## How substrate selection reaches the grader today

Substrate selection is not a wire-carried enum. The shipping mechanism is: `grader.name: grader_rpc` on `RunConfig.grader` selects the standalone-grader transport; `expose_substrate: true` on the runner opens the read-only `SubstrateService` surface the standalone grader dials; and `GraderCompositeDispatch` inside the standalone image is hard-wired to construct `LiveRunnerCallbackGradingSubstrate` per trial (see `tolokaforge/grader/composite_dispatch.py`). The in-runner counterpart, selected by `grader.name: runner_rpc`, hard-wires `InProcessGradingSubstrate`. Reserved substrates that ship later either register alongside under `tolokaforge.grading_substrates` and add their own selector on `GraderConfig` (a typed subblock, same shape as the shipped `queue` / `judge` subblocks), or ride under a new grader-name registration on `tolokaforge.trial_graders`.

## Reserved future substrate — `TrajectoryStorageGradingSubstrate`

**When to ship:** trajectory-storage service is live and stable. Grader wants to grade completed trials whose runners have been torn down (bulk rescoring, cross-run analysis).

**Wiring recipe:**

1. Implement `TrajectoryStorageGradingSubstrate(client: TrajectoryStorageClient, trial_id: str)`. Its methods delegate to storage-service RPCs: `client.get_trial_db(trial_id) -> DBSnapshot`, `client.read_trial_file(trial_id, path) -> bytes`, etc.
2. Register under the shipping entry-point group:

   ```toml
   [project.entry-points."tolokaforge.grading_substrates"]
   trajectory_storage = "tolokaforge.core.grading.substrate:TrajectoryStorageGradingSubstrate"
   ```

3. Extend `GraderConfig` with a `trajectory_storage_address: str | None = None` field so operators name the storage service, and extend `GraderCompositeDispatch` to consult that field before falling back to `LiveRunnerCallbackGradingSubstrate`. The grader-name stays `grader_rpc`; the composite dispatch picks the substrate — no runtime selector on the wire.
4. Ship as a separate PR against `main`, coordinated with the trajectory-storage team.

**No changes to evaluator code, `runner_rpc`, or the aggregate image path.**

## Reserved future substrate — `SnapshotGradingSubstrate` (Harbor pattern)

**When to ship:** offline replay / cross-region grading becomes a hard requirement (grader outlives the runner, or lives in a different network region).

**Wiring recipe:**

1. Extend `grader.proto` to wire v3 with additive snapshot-bundle fields on `GradeRequest`:
   - `initial_state_json`, `final_state_json` — DB snapshots at trial start / end.
   - `filesystem_snapshot: bytes` — tar of agent-visible files (already filtered by the runner's `_read_agent_visible_filesystem` — never `node_modules`, `.venv`, `.git`).
   - `checks_module_bytes: bytes` — `checks.py` bytes when `custom_checks` is enabled.
   - `kb_snapshot` — for tasks with deterministic KB corpora, the vector-store manifest. Live-TypeSense tasks stay on live-callback.

   `id_fields`, `unstable_fields`, and `judge_model_config` already ride on `task_description_json` / `judge_model_config_json` from wire v2 and need no addition.
2. Implement `SnapshotGradingSubstrate` unpacking the fields into the same shape `LiveRunnerCallbackGradingSubstrate` produces.
3. Register under the shipping entry-point group:

   ```toml
   [project.entry-points."tolokaforge.grading_substrates"]
   snapshot = "tolokaforge.core.grading.substrate:SnapshotGradingSubstrate"
   ```

4. **Filesystem cap policy** — 32 MB soft cap. Tasks exceeding the cap auto-fall-back to `LiveRunnerCallbackGradingSubstrate` (documented behaviour; not a bug). Config: extend `GraderConfig` with a typed `snapshot: SnapshotGraderConfig | None` subblock — same shape as the shipped `queue` / `judge` subblocks — carrying an explicit `fallback_on_snapshot_error` field (default `live_callback`).
5. The snapshot builder replaces `LiveRunnerCallbackGradingSubstrate` construction inside a new `GraderRPCSnapshotTrialGrader` (or an extended `GraderRPCTrialGrader`) — no wire-carried selector; the client-side transport decides.

**Why this is riskier than live-callback:** coding tasks with ~100 MB workspaces would need the auto-fallback path; a bug in the size check could break existing pipelines. Shipping later when the platform has learned the wire behaviours.

## Reserved future substrate — `SharedMountGradingSubstrate` (SWE-bench pattern)

**When to ship:** a single-host high-throughput deployment wants a separate grader container but doesn't want the wire hop.

**Wiring recipe:**

1. Implement `SharedMountGradingSubstrate(mount_root: Path, trial_id: str)`. Reads from a shared filesystem/DB mount populated by the runner.
2. Register under the shipping entry-point group:

   ```toml
   [project.entry-points."tolokaforge.grading_substrates"]
   shared_mount = "tolokaforge.core.grading.substrate:SharedMountGradingSubstrate"
   ```

3. Extend the standalone compose recipe with `volumes:` shared between runner + grader.
4. **Constraint:** grader and runner must be on the same host. Documented.
5. Extend `GraderConfig` with a typed `shared_mount: SharedMountGraderConfig | None` subblock carrying the mount root; `GraderCompositeDispatch` consults it and constructs `SharedMountGradingSubstrate` when set, falling back to `LiveRunnerCallbackGradingSubstrate` otherwise. Same shape as the other reserved-substrate subblocks.

## References

- [`docs/GRADER_SERVICE.md`](../GRADER_SERVICE.md) — operator-facing surface.
- [`docs/GRADER_SERVICE.md#parity-gate`](../GRADER_SERVICE.md#parity-gate) — acceptance enforcement.
- [`docs/adr/0038-grader-detachment.md`](0038-grader-detachment.md) — predecessor ADR.
- [Harbor `verifier.environment_mode = "separate"`](https://www.harborframework.com/docs/tasks)
- [Inspect AI scorer callback + deferred scoring](https://inspect.aisi.org.uk/scorers.html)
- [Braintrust sandboxed scorer](https://www.braintrust.dev/docs/platform/functions/scorers)
- [SWE-bench harness](https://www.swebench.com/SWE-bench/reference/harness/)
- [METR task standard](https://github.com/METR/task-standard)
