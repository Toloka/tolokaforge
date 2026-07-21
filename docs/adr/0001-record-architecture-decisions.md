# 0001. Record architecture decisions in ADRs

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** Tolokaforge maintainers
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge has accumulated a set of architecturally significant decisions — the adapter plugin model, the gRPC service split, the per-provider preset registry, the single-secret-abstraction rule — that today live implicitly across `AGENTS.md`, scattered `docs/*.md` files, and commit history. New contributors (human and agent) re-derive the rationale every time, and proposed changes can't be cleanly weighed against the original forces because those forces aren't written down.

We need a low-friction, durable place to capture *why* an architectural choice was made, not just *what* it is.

## Decision Drivers

- Decisions must survive the people who made them.
- Format must be agent-friendly: plain markdown, in-repo, no toolchain.
- Each decision must be individually addressable (linkable, supersedable).
- Cost of writing one must be low enough that we actually do it.

## Considered Options

1. **In-repo Markdown ADRs (MADR-style).** One file per decision under `docs/adr/`, numbered, kept forever.
2. **Wiki / Notion / Confluence.** Centralised, searchable — but lives outside the repo, drifts from code, hostile to agents reading the codebase.
3. **Long-form `ARCHITECTURE.md`.** Everything in one file. Simple, but conflates the *current* shape with the *history* of how we got there; status transitions are awkward.
4. **No formal record.** Continue relying on `AGENTS.md` and commit messages.

## Decision

We will adopt **Option 1: in-repo MADR-style ADRs** under [`docs/adr/`](.), using the template in [`0000-template.md`](0000-template.md).

Each ADR is a single Markdown file with a monotonically increasing four-digit prefix, never renumbered. Status moves through `Proposed → Accepted → (Deprecated | Superseded by ADR-NNNN)`. The current-state architecture continues to live in [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md); ADRs capture the *transitions and rationale*.

## Consequences

### Positive

- Decisions are version-controlled alongside the code they govern.
- Agents reading the repo encounter rationale in the same place as architecture diagrams.
- Superseding a decision is explicit (status flip + forward link) rather than silent drift.
- No tooling investment required.

### Negative / Trade-offs

- Discipline cost: someone has to remember to write the ADR when a significant decision is made. Mitigated by listing the prompt in `docs/adr/README.md` and in PR review checklists.
- ADRs can drift from reality if not maintained — but so can any documentation, and the in-repo location makes drift easier to catch in code review.

### Follow-ups

- Code changes required: none.
- Documentation to update: link `docs/ARCHITECTURE.md` from the top-level `README.md` Documentation table.
- Tests to add: none.
- Backfill: significant existing decisions (adapter plugin model, gRPC decomposition, secret abstraction) should be written up as ADRs over time — but not in this PR, to keep this one reviewable.

## Links

- Format reference: [adr.github.io/madr](https://adr.github.io/madr/)
- Architecture overview: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)
