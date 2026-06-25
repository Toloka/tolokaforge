# Architecture Decision Records

This directory records architecturally significant decisions about Tolokaforge. We follow the [Markdown ADR (MADR)](https://adr.github.io/madr/) format, lightly adapted.

## When to write an ADR

Write one when a decision:

- changes a boundary in the [Building Block View](../README.md#5-building-block-view-c4-level-2--container),
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
| [0003](0003-trial-spec-and-trial-result.md) | TrialSpec and TrialResult as the typed control↔trial seam | Proposed |
| [0004](0004-trial-artifact-writer-seam.md) | `TrialArtifactWriter` as the typed data-plane seam | Proposed |
| [0005](0005-run-aggregate-writer-seam.md) | `RunAggregateWriter` as the run-level data-plane seam | Proposed |
