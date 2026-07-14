---
name: writing-development-tickets
description: >
  Use when creating development tickets as GitHub Issues, scoping work for a feature area,
  or brainstorming what tickets are needed for a new area. Covers ticket granularity,
  required labels, and content structure.
---

# Writing Development Tickets

Tickets are GitHub Issues in the `Toloka/tolokaforge` repository.
Each ticket = one coherent deliverable for a human+AI pair, taking 1-5 days.

## Ticket Granularity

**Good ticket = one coherent feature, independently deliverable, 3-15 implementation steps.**

| Good | Bad (too small) | Bad (too big) |
|------|-----------------|---------------|
| "Rubric-judge trust gate" | "Add a field to GradingConfig" | "Rework the grading pipeline" (7 streams) |
| "Per-provider schema sanitizer for X" | "Rename one preset key" | "Support every OpenRouter model" |
| "Task-pack validation CLI" | "Add one validator function" | |

**Rule of thumb:** Can't explain in 2 sentences without listing steps → too big. Deliverable IS a step → too small.

### When to Split
- Deliverables span unrelated systems
- Different people could work on parts independently
- One part could ship without the other
- More than ~15 implementation steps

### When NOT to Split
- Parts are tightly coupled (schema + preset + capability test for the same model)
- Splitting creates more coordination overhead than the ticket itself
- One part is useless without the other

## GitHub Issue Structure

### Title
Action-oriented, describes the deliverable: `feat: Add pipeline batch progress tracking`

### Labels

Use the repo's existing taxonomy:

| Label | Usage |
|-------|-------|
| `enhancement` | New capability |
| `bug` | Something broken |
| `documentation` | Documentation only |
| `question` | Needs clarification before it's actionable |

The repo has no priority labels — record priority in the issue body as a
`**Priority:** P0–P3` line. The `automation:*` labels are owned by the
model auto-integration workflows; never set them by hand.

### Issue Body Content

**Required:**
- **Context** (2-3 sentences): What problem, why now. Only what's unique to this deliverable.
- **Priority**: P0–P3, one line.
- **Deliverables**: Checkboxed list of what's true when done
- **Acceptance criteria**: User-observable verification (not code-level)
- **Design decisions**: Chosen approaches with rationale ("X because Y")

**Optional:**
- Scope boundary (what's explicitly OUT)
- Dependencies (other issues)
- Open questions (for design phase)

**What does NOT go in a ticket:**
- File paths, function signatures, class structure
- Schema definitions, endpoint specs, implementation steps
- Code snippets or pseudocode

**Why?** Implementation details rot. Codebase changes between ticket creation and execution. Design happens just-in-time during execution workflow.

### Issue Template

```markdown
## Context
<What problem this solves, why now — 2-3 sentences>

**Priority:** P1

## Deliverables
- [ ] <What's true when done — user-observable>
- [ ] <Another deliverable>

## Acceptance Criteria
- <How to verify this works>
- <Another verification>

## Design Decisions
- **<Decision>**: <Rationale> ("X because Y")

## Out of Scope
- <What is NOT included in this ticket>
```

## Ticket Lifecycle

| Status | Meaning | Maps to |
|--------|---------|---------|
| Open | Defined, ready for work | Backlog / Ready |
| In Progress | Implementation underway | Assigned, branch created |
| In Review | PR created | PR linked |
| Closed | Merged or resolved | Done |

## Creating Issues via GitHub MCP

Use the GitHub MCP `issue_write` tool:

```
method: create
owner: Toloka
repo: tolokaforge
title: "feat: <description>"
body: "<issue template filled in>"
labels: ["enhancement"]
```

## Creation Checklist

- [ ] Ticket is at feature level (not too small, not too big)
- [ ] Type label set (enhancement/bug/documentation)
- [ ] Priority stated in the body (P0-P3)
- [ ] Context explains "why now"
- [ ] Deliverables are checkboxed and user-observable
- [ ] Design decisions list chosen approaches with rationale
- [ ] No implementation details leaked in

## Anti-Patterns

| Anti-Pattern | Problem |
|-------------|---------|
| Implementation plan as ticket | Steps != deliverables. Ticket is the *what*, not the *how* |
| One ticket per file change | Too granular — think in capabilities |
| Epic disguised as ticket | 3+ weeks, 5+ independent deliverables = split it |
| Stale implementation notes | Endpoint specs from 2 months ago are wrong now |
| Priority mismatch | P3 ticket for a P0 area = something is wrong |
