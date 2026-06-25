# Runtime Architecture Roadmap

Status: living document.

This is the public roadmap for Tolokaforge's runtime architecture evolution.
Long-form rationale for each decision lives in the [ADRs](adr/). Progress
against open work lives on [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
and the project boards.

## Phase ladder

The work is **one ordered sequence** — no parallel tracks. Each phase is
independently shippable and builds on the previous one.

- **Phase 1 — Boundaries & contracts** *(no behaviour change)*
  Typed seams between control plane, trial plane, and data plane. After Phase 1
  each plane has a runtime-checkable Protocol with at least two
  implementations.
- **Phase 2 — First complex adapter on the new contracts** *(single-container)*
  Prove the contracts on a workload more complex than the built-in
  `native` and `terminal_bench` adapters.
- **Phase 3 — Multi-container environments + `local` runtime backend**
  Per-trial environment manifest + provisioner; `RuntimeBackend` interface
  with `local` (docker-compose-per-trial) as the first backend.
- **Phase 4 — Multi-container adapter** *(complex adapter on the new env)*
  Express adapters whose tasks need a database + service + frontend as a
  multi-container manifest, materialised by the runtime backend.
- **Phase 5 — Remote runner** *(orchestrator ≠ runner machine)*
  Runner reachable over the network with mTLS, health, and retry. Directly
  answers the "split orchestrator and runner" pain.
- **Phase 6 — Control plane + backend-agnostic scheduler**
  Control-plane API + state store + scheduler dispatching trials through the
  `RuntimeBackend` interface. The control plane is backend-agnostic, so
  adding an at-scale backend later needs no control-plane change.

**Later (directional, not committed).** Beyond Phase 6 the direction is
set but scope may shift as infrastructure needs evolve: an at-scale runtime
backend (Kubernetes / Agent Sandbox), autoscaling and centralized
observability, shared-cluster operation with quotas and per-team secrets,
mid-trial resume with warm pods, a durable workflow engine if reliability
demands it, and additional runtime backends (microVM, Modal, EC2).

## Release alignment

| Release   | Phases              | Architectural scope (what ships)                                                                                                                                                                                                                                  | Status     | ADRs                       |
|-----------|---------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------|----------------------------|
| **0.3.0** | Phase 1 (seed)      | Open `AdapterType` for plugin adapters; `TrialSpec` / `TrialResult` typed control↔trial seam; `TrialArtifactWriter` per-trial data-plane seam; `RunAggregateWriter` run-level data-plane seam; typed `EnvEndpoints`; runner `tool_factory` decoupled from adapter layouts. | In flight  | [0003](adr/0003-trial-spec-and-trial-result.md), [0004](adr/0004-trial-artifact-writer-seam.md), [0005](adr/0005-run-aggregate-writer-seam.md) |
| 0.4.0     | Phase 1 (close) + Phase 2 | Remaining Phase 1 seams (`RuntimeBackend` Protocol, `Conductor` extraction). First complex adapter ported onto the new contracts.                                                                                                                                  | Planned    | tbd                        |
| 0.5.0     | Phase 3             | Environment manifest + multi-container `local` runtime backend. Per-trial isolation.                                                                                                                                                                              | Planned    | tbd                        |
| 0.6.0     | Phase 4             | Multi-container adapter (database + service + frontend) graded on the new environment.                                                                                                                                                                            | Planned    | tbd                        |
| 0.7.0     | Phase 5             | Remote runner: orchestrator and runner on separate hosts; mTLS + health + retry.                                                                                                                                                                                  | Planned    | tbd                        |
| 0.8.0+    | Phase 6             | Control plane API + state store; backend-agnostic scheduler.                                                                                                                                                                                                       | Planned    | tbd                        |

Versions past the current release are **targets, not commitments** — scope
may shift between phases as we land each one. The phase order is fixed.

## How to follow progress

- **Decisions:** ADRs in [`adr/`](adr/) — each architectural seam lands as a
  Proposed ADR before code, and is marked Accepted once shipped.
- **Open work:** [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
  tagged `architecture` and the project board.
- **Released changes:** [`CHANGELOG.md`](../../CHANGELOG.md) at repo root.

## Updating this doc

This file is updated **on release events only** — when a release closes
(move the row's status to `Shipped`, add a new `In flight` row for the next
version) or when scope moves between phases. PR-level status does not
belong here; it lives on GitHub issues and the project board.
