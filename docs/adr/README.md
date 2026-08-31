# Architecture Decision Records

This directory records architecturally significant decisions about Tolokaforge. We follow the [Markdown ADR (MADR)](https://adr.github.io/madr/) format, lightly adapted.

## When to write an ADR

Write one when a decision:

- changes a boundary in the [Building Block View](../ARCHITECTURE.md#5-building-block-view-c4-level-2--container),
- introduces or removes a cross-cutting rule,
- locks in a quality trade-off (e.g. determinism vs. flexibility, isolation vs. simplicity),
- or replaces an earlier ADR.

Day-to-day implementation choices that don't affect the system shape do *not* need an ADR — a clear PR description is enough.

## How to write one

1. Copy [`0000-template.md`](0000-template.md) to `NNNN-kebab-case-title.md`, where `NNNN` is the next free number. **Never renumber existing ADRs.**
2. Fill in the sections. Keep "Context" focused on the forces that drove the decision, not the implementation.
3. An ADR implemented by the PR that introduces it lands `Accepted` — the decision is in effect the moment that PR merges. `Proposed` is for a design-first ADR opened ahead of its implementation, and flips to `Accepted` when the implementing PR merges.
4. If a later decision overrides this one, set the old ADR's status to `Superseded by ADR-NNNN` and add the back-link in the new ADR's "Decision Drivers".

## Statuses

- `Proposed` — under discussion in a PR.
- `Accepted` — merged and in effect.
- `Deprecated` — no longer applies, but not replaced by anything specific.
- `Superseded by ADR-NNNN` — replaced; keep the old file as historical record.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions in ADRs | Accepted |
| [0002](0002-external-model-registry.md) | External model registry — operator-overridable preset data | Accepted |
| [0003](0003-trial-spec-and-trial-result.md) | TrialSpec and TrialResult as the typed control↔trial seam | Accepted |
| [0004](0004-trial-artifact-writer-seam.md) | `TrialArtifactWriter` as the typed data-plane seam | Accepted |
| [0005](0005-run-aggregate-writer-seam.md) | `RunAggregateWriter` as the run-level data-plane seam | Accepted |
| [0006](0006-typed-env-endpoints.md) | `EnvEndpoints` — typed runner service URLs on `TrialSpec` | Accepted |
| [0007](0007-runtime-backend-protocol.md) | `RuntimeBackend` Protocol — lift `SharedStackRuntimeBackend` behind a typed seam | Accepted |
| [0008](0008-conductor-protocol.md) | `Conductor` Protocol — per-trial executor seam | Accepted |
| [0009](0009-environment-manifest.md) | `EnvironmentManifest` — typed schema for per-trial multicontainer environments | Accepted |
| [0010](0010-runtime-backend-provisioning-contract.md) | `RuntimeBackend` provisioning contract — `provision` / `await_ready` / `endpoints` / `teardown` | Accepted |
| [0011](0011-seam-and-declaration-conventions.md) | Seam-definition and data-declaration conventions for new components | Accepted |
| [0012](0012-custom-checks-extension.md) | `CheckExecutor` Protocol — the custom-checks extension seam | Accepted |
| [0013](0013-runtime-backend-per-trial-rpc-methods.md) | `RuntimeBackend` owns per-trial RPC methods — collapse `DockerRunnerAdapter` | Accepted |
| [0014](0014-trial-grader-protocol.md) | `TrialGrader` Protocol — swappable trial-grading strategy | Accepted |
| [0015](0015-trial-executor-protocol.md) | `TrialExecutor` Protocol — per-trial substrate-lifecycle seam | Accepted |
| [0016](0016-runtime-backend-comparison.md) | Runtime backend comparison: `shared` vs `per_trial` (lifecycle axis) | Accepted |
| [0017](0017-persistent-agent-shell-and-editor-tools.md) | Persistent agent shell + first-class editor tools + tool-lifecycle evolution | Proposed |
| [0018](0018-multi-container-under-shared-runtime.md) | Multi-container capability under shared runtime (composition axis) | Accepted |
| [0019](0019-front-end-plugin-namespace.md) | Front-end pluggability via `tolokaforge.dx` | Accepted |
| [0020](0020-judge-protocol.md) | `Judge` Protocol — the grading-plane judge seam | Accepted |
| [0021](0021-component-monitoring-seam.md) | Component-oriented monitoring — `ComponentSnapshot` / `component_*` events, panel widget with auto-expand-on-fail | Accepted |
| [0022](0022-runtime-independence.md) | Runtime independence — Protocol registries, `run_trial`, `run-trial` subprocess contract | Accepted |
| [0023](0023-runner-image-internals.md) | Runner image internals — monolithic wheel + `[runner]` extra, internals not a stability commitment | Accepted |
| [0024](0024-container-command-surface.md) | Container command surface — the committed contract of `tolokaforge-runner` | Accepted |
| [0025](0025-runner-wheel-split.md) | Runner wheel split — slim subset artifact + `_runner_subset` enumeration | Accepted |
| [0026](0026-service-readiness-contract.md) | Service-readiness contract as a fourth entry-point-registry seam | Accepted |
| [0027](0027-subset-native-cli-shim.md) | Subset-native CLI shim | Accepted |
| [0028](0028-multi-actor-turn-policy.md) | Multi-actor turn policy — `interaction_mode` + `Actor` + `TurnPolicy` | Accepted |
| [0029](0029-build-check-builtin-tool.md) | `build_check` as a generic peer-service HTTP probe in core | Accepted |
| [0030](0030-tolokaforge-models-split.md) | Model data as a second PyPI wheel — `tolokaforge-models` from the same monorepo | Proposed |
| [0031](0031-pull-vs-build-default-for-service-images.md) | Wheel consumers pull published images by default — `docker.image_source` policy | Proposed |
| [0032](0032-agent-completion-is-structural.md) | The agent's completion is structural; `###STOP###` is the user simulator's | Accepted |
| [0033](0033-external-harness-registry.md) | External harness registry — operator-overridable YAML for coding-CLI parity knobs | Accepted |
| [0034](0034-external-harness-plugin-discovery.md) | External harness plugin discovery — pip-installable harness bundles | Accepted |
| [0035](0035-idle-turn-heuristic-deleted.md) | Whether an agent must act is a per-task assertion, not a stuck heuristic | Accepted |
| [0036](0036-tolokaforge-coding-harnesses-split.md) | Coding-harness code as a top-level workspace package — `tolokaforge_coding_harnesses` | Accepted |
| [0037](0037-runtime-gateway-as-harness-data.md) | A runtime gateway is harness data, and its token dialect belongs to the runtime that provisions it | Accepted |
| [0038](0038-grader-detachment.md) | Grader detachment — grader as an independently deployable and scalable component | Proposed |
| [0039](0039-coding-harness-adapter-agnostic.md) | Coding-harness as an adapter-agnostic run-config concept | Accepted |
| [0040](0040-standalone-grader.md) | Standalone-grader substrate — multi-topology grading behind one Protocol | Accepted |
| [0041](0041-zero-coverage-exit-signal.md) | Zero-coverage exit signal on `run_state.json` | Accepted |
| [0042](0042-adapter-blind-authoring-gate.md) | Adapter-blind authoring gate — three new `BaseAdapter` hooks + `SkipKind` split | Accepted |
| [0043](0043-hybrid-runtime-backend.md) | HybridRuntimeBackend — shared engine services + per-trial task compose | Proposed |
