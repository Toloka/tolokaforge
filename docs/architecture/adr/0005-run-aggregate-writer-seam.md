# 0005. `RunAggregateWriter` as the run-level data-plane seam

- **Status:** Proposed
- **Date:** 2026-06-23
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine's architecture has three planes:

- **Control plane** — schedules trials, tracks state.
- **Trial plane** — runs one trial end-to-end (agent loop, tool calls, grading).
- **Data plane** — persists run outputs (per-trial artifacts; run-level aggregates) for downstream readers.

ADR-0004 typed the **per-trial** half of the data plane behind `TrialArtifactWriter`. The **run-level** half is still inline disk I/O: `Orchestrator._generate_reports` opens four `*.json` files directly and `json.dump`s into them. There is no seam, no alternative implementation, no contract test pinning what the orchestrator emits at the run-output root.

ADR-0004's `Follow-ups` section explicitly listed this gap:

> `RunAggregateWriter` seam (separate from this). The orchestrator writes post-run aggregates at the run-output root, outside any trial directory. Those are run-level concerns, not per-trial; they belong behind their own seam if/when a non-filesystem run-level destination is needed.

This ADR closes that follow-up. After it lands, every plane has a named, typed, runtime-checkable contract with at least two implementations.

## Decision Drivers

- **Symmetry across the three planes.** Per-trial artifacts already sit behind a runtime-checkable Protocol with two implementations. The run-level half should match.
- **One seam per lifecycle.** Per-trial writes happen *as each trial completes*; run aggregates write *once at the end*. Mixing them in one Protocol would force every backend to handle two different write rhythms (high-rate per-trial vs single-shot run-level).
- **Lean code.** The four `open() + json.dump()` blocks in `_generate_reports` are five lines apiece, repeat the same serializer kwargs, and obscure the orchestrator's actual responsibility (assembling the dicts). A writer collapses them to one call.
- **Plug-in discovery later.** Entry-point-discovered backends (`tolokaforge.run_aggregate_writers`-style) need `isinstance` to work for safe injection — same argument as ADR-0004.
- **Fail-fast.** No silent fallbacks on a malformed writer; runtime check is the cleanest line of defence.

## Considered Options

1. **Add `RunAggregateWriter` Protocol + `FileAggregateWriter` + `InMemoryAggregateWriter`, route the orchestrator's four writes through the bundle method, ship a canonical contract test + an on-disk layout test.** Closes all gaps with one focused change.
2. **Only add `FileAggregateWriter` (no Protocol, no second impl).** Cheapest — collapses the four `json.dump` blocks behind one class. Leaves the seam unproven (no second backend, no runtime-checkable contract).
3. **Extend `TrialArtifactWriter` with run-level methods instead of a new Protocol.** Single Protocol, single class hierarchy. Mixes two lifecycles into one contract — every backend now has to handle both rhythms.
4. **Defer until a non-filesystem backend is requested.** Each later contract change (entry-point discovery, remote object store, async writers, typed Pydantic aggregates) ends up co-litigating the run-level shape simultaneously with whatever else is in flight.

## Decision

We will adopt **Option 1**.

- Add `RunAggregateWriter` as a `@runtime_checkable` `typing.Protocol` in `tolokaforge/core/output/aggregates.py` with five methods: one per-piece writer for each of the four JSON files (`write_per_task_metrics`, `write_aggregate`, `write_metadata_slices`, `write_failure_attribution`) and a `write_run_aggregates` convenience that writes all four.
- Add `FileAggregateWriter` — the disk-backed implementation. Each method opens `output_dir / "<name>.json"` and `json.dump`s with `indent=2, default=str` — byte-identical to the pre-PR writes the orchestrator did inline.
- Add `InMemoryAggregateWriter` — a non-disk implementation that records each run's artifacts on a `RunAggregateBundle` dataclass keyed by `output_dir`. Two purposes: (a) prove the Protocol is swappable; (b) serve as a test fixture for code that needs a writer but should not touch the filesystem.
- Route the orchestrator's four inline writes in `_generate_reports` through a single `self._run_aggregate_writer.write_run_aggregates(...)` call. The dict-assembly logic (`schema_version` injection, failure-attribution envelope, empty-results early return) stays in the orchestrator — the writer is downstream of dict assembly.
- Add canonical tests:
  - `tests/canonical/test_run_aggregate_writer_contract.py` — Protocol conformance + parity between the two implementations.
  - `tests/canonical/test_run_aggregate_layout.py` — on-disk layout assertions (filenames, top-level shape, envelope keys, serializer conventions).
  - `tests/unit/test_output_run_aggregates.py` — focused unit tests for both writers.

## Consequences

### Positive

- The data plane has *both* halves behind typed, runtime-checkable seams — per-trial and run-level — matching the symmetry of the control / trial planes.
- The four `open() + json.dump()` blocks in the orchestrator collapse to one writer call. `_generate_reports` is now solely about dict assembly + an envelope; storage is a single delegated line.
- `InMemoryAggregateWriter` is reusable by any test that needs to assert on what the orchestrator emitted at run-end without JSON-parsing temp files.
- Future entry-point-registered backends can rely on `isinstance(writer, RunAggregateWriter)` instead of structural-only type checks.
- An external backend (S3, GCS, analytics service) can be built against the Protocol with no engine changes — `InMemoryAggregateWriter` serves as a structural template.

### Negative / Trade-offs

- The Protocol's surface today (raw `dict[str, Any]` payloads) reflects how the orchestrator builds the aggregates today, not an idealised shape. Typing the four payloads with Pydantic models is worthwhile, but it's a separate ADR with its own breaking-change risk for downstream readers.
- The four per-piece methods are partly redundant with the `write_run_aggregates` bundle method. Keeping per-piece methods on the Protocol forces alternate implementations to provide them, but the win — Protocol matches both call shapes (bundle for orchestrator, per-piece for ad-hoc tooling) — outweighs the cost.
- The orchestrator gains a new private attribute (`self._run_aggregate_writer`). Mirrors the existing `self._artifact_writer` precedent.

### Follow-ups

- **Entry-point discovery for run-aggregate writers.** Mirror the `tolokaforge.adapters` pattern with a `tolokaforge.run_aggregate_writers` entry-point group so operators can inject a writer via config.
- **Typed Pydantic models for the four aggregates.** `per_task_metrics`, `aggregate`, `metadata_slices`, `failure_attribution` today are wide, inconsistent across slices, and produced by metric-calc functions that return `dict[str, Any]`. Typing them is its own ADR.
- **Async variants.** Today's writers are sync; if a remote backend's run-end write latency starts to matter, an `AsyncRunAggregateWriter` Protocol can be added without touching the sync one.
- **Concrete remote backend (S3 / GCS).** User-driven; the seam is now ready for one without engine edits.
- **Streaming / per-trial flushes.** Today's writers are called once at run-end. If a consumer needs progress visibility before the run finishes, a streaming variant is its own design conversation.

## Rejected alternatives

- **Option 2 — only add `FileAggregateWriter` (no Protocol, no second impl).** Collapses the inline writes but leaves the seam undocumented and untestable as a contract. Half-measure.
- **Option 3 — extend `TrialArtifactWriter`.** Conflates per-trial and run-level lifecycles. A backend tuned for high-rate per-trial writes (e.g. local disk with a cached `OutputWriter` per trial) is the wrong shape for a single-shot run-level upload, and vice versa. Two seams keep each Protocol minimal.
- **Option 4 — defer.** Each later contract change ends up co-litigating the run-level shape simultaneously with whatever else it's doing. Cheaper to settle the seam now while there's only one implementation to migrate.

## Scope notes

- **Analytics artifacts only.** `run_state.json` (durable resume state, written by `RunStateManager`), `engine_run_state.json`, and `run_queue.sqlite` (the durable attempt queue) all live at the run-output root but are **infrastructure**, not analytics. They belong to the resume/queue layer and stay out of this seam.
- **`_generate_reports`'s dict-assembly logic is unchanged.** The orchestrator still computes per-task metrics, the aggregate, the metadata slices, and the failure attribution. The writer is only the disk-touching tail. Future ADRs that retype the payloads (Pydantic-modelled aggregates) will live in `metrics.py` / `failure_attribution.py`, not in this seam.
- **Empty-results early return is preserved.** When `self.results` is empty, `_generate_reports` returns at the top with a warning and **no** aggregate file is written. The writer is never invoked in that case. Behaviour-equivalent with the pre-PR code path.
