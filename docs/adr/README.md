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
3. Open the PR with status `Proposed`. Flip to `Accepted` when merged.
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
| [0002](0002-external-model-registry.md) | External model registry — operator-overridable preset data | Proposed |
| [0003](0003-trial-spec-and-trial-result.md) | TrialSpec and TrialResult as the typed control↔trial seam | Accepted |
| [0004](0004-trial-artifact-writer-seam.md) | `TrialArtifactWriter` as the typed data-plane seam | Accepted |
| [0005](0005-run-aggregate-writer-seam.md) | `RunAggregateWriter` as the run-level data-plane seam | Accepted |
| [0006](0006-typed-env-endpoints.md) | `EnvEndpoints` — typed runner service URLs on `TrialSpec` | Accepted |
| [0007](0007-runtime-backend-protocol.md) | `RuntimeBackend` Protocol — lift `SharedStackRuntimeBackend` behind a typed seam | Accepted |
| [0008](0008-conductor-protocol.md) | `Conductor` Protocol — per-trial executor seam | Accepted |
| [0009](0009-environment-manifest.md) | `EnvironmentManifest` — typed schema for per-trial multicontainer environments | Accepted |
| [0010](0010-runtime-backend-provisioning-contract.md) | `RuntimeBackend` provisioning contract — `provision` / `await_ready` / `endpoints` / `teardown` | Accepted |
| [0011](0011-seam-and-declaration-conventions.md) | Seam-definition and data-declaration conventions for new components | Proposed |
| [0013](0013-runtime-backend-per-trial-rpc-methods.md) | `RuntimeBackend` owns per-trial RPC methods — collapse `DockerRunnerAdapter` | Accepted |
| [0014](0014-trial-grader-protocol.md) | `TrialGrader` Protocol — swappable trial-grading strategy | Accepted |
| [0015](0015-trial-executor-protocol.md) | `TrialExecutor` Protocol — per-trial substrate-lifecycle seam | Accepted |
| [0016](0016-runtime-backend-comparison.md) | Runtime backend comparison: `shared` vs `per_trial` (lifecycle axis) | Accepted |
| [0017](0017-persistent-agent-shell-and-editor-tools.md) | Persistent agent shell + first-class editor tools + tool-lifecycle evolution | Proposed |
| [0018](0018-multi-container-under-shared-runtime.md) | Multi-container capability under shared runtime (composition axis) | Accepted |
