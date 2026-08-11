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

Every ticket gets one type label and one priority label.

| Type label | Usage |
|-------|-------|
| `enhancement` | New capability |
| `bug` | Something broken |
| `documentation` | Documentation only |
| `question` | Needs clarification before it's actionable |

| Priority label | Usage |
|-------|-------|
| `P0` | Critical: blocks production or core user flow |
| `P1` | High: important UX, stability or correctness gap |
| `P2` | Medium: improvement or hardening, not user-blocking |
| `P3` | Low: cleanup, nitpick, deferred improvement |

The `automation:*` labels are owned by the model auto-integration
workflows; never set them by hand.

### Issue Body Content

**Required:**
- **Context** (2-3 sentences): What problem, why now. Only what's unique to this deliverable.
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
labels: ["enhancement", "P2"]
```

## Creation Checklist

- [ ] Ticket is at feature level (not too small, not too big)
- [ ] Type label set (enhancement/bug/documentation)
- [ ] Priority label set (P0-P3)
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

## Umbrella issue template

An **umbrella issue** is the GitHub-side companion to a milestone. One umbrella per milestone, titled `[umbrella] <milestone-slug> — <name>`. It hosts the milestone's scope, sub-issue list, sequencing constraints, and — after the milestone runs — the merge log that `/implement-milestone` appends to. Feature tickets are children; the umbrella is the parent.

Use this template when creating a new umbrella:

```markdown
## Problem
<2–4 sentences. What is broken, missing, or awkward that this milestone exists to fix. Concrete enough that a reader who never worked on the codebase can hold the picture.>

## Product outcome
<One paragraph. What changes for users (data-labelling teams, adapter authors, or contributors) when this milestone lands. User-facing, not implementation-facing.>

## Key decisions surfaced
<Enumerate the yes/no or fork-in-the-road choices this milestone forces. One line each with a leaning. These are the decisions a PM must settle before the pipeline runs — they map 1-to-1 with the `## Decisions needed before implementation` block the architect will emit at plan time. Surface them here so the milestone can be scheduled with fewer mid-run stalls.>

- **<Decision label>** — <question>. Leaning: <default>. Impact if reversed: <one line>.
- ...
- Or: "None — mechanical execution of a settled design."

## Sub-issues (phases)
<Group child issues by phase. Each phase is a slice you could ship in a single per-issue pipeline pass. Sequencing constraints go here, not in the individual tickets.>

### Phase 1 — <name>
- [ ] #<N> <title>
- [ ] #<N> <title>

### Phase 2 — <name>
- [ ] #<N> <title>

## Sequencing constraints
<Bullets. "Phase 2 depends on Phase 1's <specific contract>", "issue #X must land before #Y because …". Explicit — don't rely on issue order.>

## Out of scope
<Bullets. Anything a reader might reasonably expect to be part of this milestone but isn't. Include a one-line "why not now" for each.>

## Definition of done
<Bullets. What is true when this milestone is closed. Test-tier evidence where possible (unit / canonical / integration coverage). One clear closure criterion per bullet.>

## Educative hook
<One sentence. What a reader following the milestone's consolidation PR will *learn* — the concept, pattern, or design principle this milestone teaches. This is the seed the consolidation PR body's `Concept map` will grow into. Skip only if the milestone is purely mechanical.>
```

**Umbrella-specific rules:**

- Title format: `[umbrella] <milestone-slug> — <name>`. GitHub has no umbrella label; the title convention is the marker.
- Attach the umbrella issue to the GitHub milestone the same way as any feature ticket.
- Umbrella body updates during the milestone: `/implement-milestone` appends `#<issue> → PR #<pr> → <sha> — <outcome>` lines as issues merge. That log is load-bearing — treat the umbrella body as a running document, not a static one.
- Sub-issues are created with the atomic-feature template above. They reference the umbrella (`Part of #<umbrella>`).
- Priority label is required on the umbrella itself (usually `P1` or `P2`); child priorities can vary.
