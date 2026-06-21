# 0004. `TrialArtifactWriter` as the typed data-plane seam

- **Status:** Proposed
- **Date:** 2026-06-21
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine's architecture has three planes:

- **Control plane** — schedules trials, tracks state.
- **Trial plane** — runs one trial end-to-end (agent loop, tool calls, grading).
- **Data plane** — persists per-trial outputs (trajectory, grade, metrics, prompts, tool schemas, logs, environment state) for downstream readers (analytics, dashboards, audit).

Each plane should sit behind a named, typed seam so implementations can swap without engine edits. The control↔trial seam is being typed by `TrialSpec` / `TrialResult` (ADR-0003 lineage); a remote conductor in a different process will read the same payload the in-process one reads today.

The data-plane seam is partially built. `tolokaforge/core/output/artifacts.py` already declares `TrialArtifactWriter` as a `typing.Protocol`, and `FileArtifactWriter` is its disk-backed implementation. Several gaps prevent the seam from playing the architectural role the other two planes' contracts do:

1. The Protocol is **not `@runtime_checkable`** — `isinstance(writer, TrialArtifactWriter)` raises `TypeError` even when the writer satisfies every method signature. Other Protocols in the engine (the LLM policy chain — `SystemPromptPolicy`, `ResponsePolicy`, `ReasoningCodec`, …) are runtime-checkable; this one is the outlier.
2. The orchestrator's main entry point — `writer.write_trial_bundle(...)` — is on `FileArtifactWriter` but **not declared on the Protocol**. Alternate implementations would have to add it as an undeclared "extra" method or rebuild bundle semantics from the eight per-piece writers.
3. **Only one implementation exists.** Without a second concrete writer, there's no evidence the Protocol's surface is enough to swap in alternate backends (an in-memory test fixture, an S3 bucket, a remote object store) without engine changes.
4. **No ADR documents the seam.** A reviewer looking for "where the data-plane contract is decided" finds the Protocol declaration but no statement of intent or alternatives considered.

## Decision Drivers

- **Symmetry across the three planes.** Control↔trial and trial↔runner are typed, runtime-checkable contracts with multiple implementations; the data plane should match.
- **The Protocol must mirror real callers.** `write_trial_bundle` is the orchestrator's main call site (`orchestrator.py`). A Protocol that omits it lies about the contract.
- **Plug-in discovery later.** Entry-point-discovered writers (`tolokaforge.artifact_writers`-style) need `isinstance` to work for safe injection.
- **Fail-fast.** No silent fallbacks if an injected writer is malformed; runtime check is the cleanest line of defence.
- **Lean code.** Documentation belongs in commits / ADRs, not in source files; the implementation stays small.

## Considered Options

1. **Add `@runtime_checkable`, declare `write_trial_bundle` on the Protocol, ship an `InMemoryArtifactWriter` second implementation, write this ADR.** Closes all four gaps with one focused change.
2. **Only add `@runtime_checkable`.** Cheapest. Leaves the Protocol↔callers mismatch and the no-second-implementation gap.
3. **Build an alternate concrete backend now (S3 / GCS) instead of `InMemoryArtifactWriter`.** Proves the seam more thoroughly but requires picking a backend, writing real auth code, and shipping a feature no one has asked for.
4. **Defer everything until a remote conductor exists and forces the issue.** Deferral has zero cost today but means each follow-on (entry-point discovery, remote object store, async writers) re-litigates the contract simultaneously with whatever else it's doing.

## Decision

We will adopt **Option 1**.

- Add `@runtime_checkable` to the `TrialArtifactWriter` Protocol declaration.
- Promote `write_trial_bundle(trial_dir, trajectory, task_snapshot, env_state, logger) -> None` from `FileArtifactWriter`-only to a Protocol-declared method. `FileArtifactWriter`'s implementation is unchanged; only the Protocol declaration grows by one method.
- Add `InMemoryArtifactWriter` — a non-disk implementation that stores each trial's artifacts in `self.trials: dict[Path, TrialArtifactBundle]`. The dataclass holds slots for `trajectory`, `task`, `env`, `metrics`, `grade`, `logs`, `tools_schemas`, `prompts`. Two purposes: (a) prove the Protocol is swappable; (b) serve as a test fixture for code that needs a writer but should not touch the filesystem.
- Add a canonical contract test (`tests/canonical/test_artifact_writer_contract.py`) that asserts `isinstance(writer, TrialArtifactWriter)` for both implementations and that every Protocol method accepts the same arguments on each.

## Consequences

### Positive

- The data plane has a typed, runtime-checkable seam that matches the orchestrator's actual usage.
- The three-plane architecture (control / trial / data) now has at least one named, swappable contract per plane.
- `InMemoryArtifactWriter` is reusable by any test that needs to assert on what was written without YAML round-trips through `tmp_path`.
- Future plug-in discovery (entry-point-registered writers) can rely on `isinstance` instead of structural-only type checks.
- An external backend (S3, GCS) can be built against the Protocol with no engine changes — the `InMemoryArtifactWriter` serves as a structural template.

### Negative / Trade-offs

- Adding `write_trial_bundle` to the Protocol is a **breaking change** for any downstream implementer that had already provided a `TrialArtifactWriter` without that method. No known external implementers today; the change is breaking-by-shape only.
- The `write_trial_bundle` method is partly redundant with the six per-piece writers (it bundles them). Keeping it on the Protocol forces alternate implementations to implement it, but the win — Protocol matches caller usage — outweighs the cost.
- `InMemoryArtifactWriter` adds a maintenance surface that has to stay in sync with `FileArtifactWriter`'s method signatures. The canonical contract test catches drift.

### Follow-ups

- **Entry-point discovery for writers.** Mirror the `tolokaforge.adapters` pattern with a `tolokaforge.artifact_writers` entry-point group so operators can inject a writer via config.
- **`RunAggregateWriter` seam (separate from this).** The orchestrator writes post-run aggregates (`per_task_metrics.json`, `aggregate.json`, …) at the run-output root, outside any trial directory. Those are run-level concerns, not per-trial; they belong behind their own seam if/when a non-filesystem run-level destination is needed.
- **Async variants.** Today's writers are sync; if a remote backend's per-trial write latency starts to matter, an `AsyncTrialArtifactWriter` Protocol can be added without touching the sync one.
- **Concrete remote backend (S3 / GCS).** User-driven; the seam is now ready for one without engine edits.

## Rejected alternatives

- **Option 2 — only add `@runtime_checkable`.** Leaves `write_trial_bundle` off the Protocol, so the contract still lies about its main caller. Half-measure.
- **Option 3 — build S3 / GCS now.** Picks a backend before there's a consumer requirement, adds auth + retry concerns this PR shouldn't own. Better as a follow-on once a real need lands.
- **Option 4 — defer everything.** Each later contract change (remote conductor, plug-in discovery, async writers) ends up co-litigating the data-plane shape simultaneously with whatever else it's doing. Cheaper to settle the seam now while there's only one implementation to migrate.

## Scope notes

- **Per-trial artifacts only.** The orchestrator also writes post-run aggregate JSON files at the run-output root (`per_task_metrics.json`, `aggregate.json`, `metadata_slices.json`, `failure_attribution.json`). Those are out of scope for this seam — they're run-level concerns with different lifecycle and would warrant a separate `RunAggregateWriter` contract.
- **`FileArtifactWriter` internals are unchanged.** Two methods (`write_tools_schemas`, `write_prompts`) bypass the cached `OutputWriter` and write YAML directly. That asymmetry is pre-existing; cleaning it up is a separate refactor, not a precondition for formalising the Protocol.
