# PR body templates

Two templates, one source of truth. The per-issue template drives Step 10 of `/executing-development-tickets`; the consolidation template drives Step 4 of `/implement-milestone`. Both are read by three audiences — the human reviewer, future agents planning follow-up work, and (for per-issue PRs) the milestone consolidation body that cites them.

Discipline shared by both templates (modelled on [Toloka/tolokaforge#121](https://github.com/Toloka/tolokaforge/pull/121)):

- Flat outline: `##` and `###` only, no deeper.
- No emoji-heavy section names, no decorative headers.
- No AI-attribution footer.
- Every backticked type / field / preset / file path renders as code.
- Diagrams use fenced ```mermaid``` blocks — GitHub renders them natively.
- No internal names in the body: no private repo names, no private adapter or library names, no internal ticket IDs. Link from the internal side instead.

---

## 1. Per-issue PR body — for PRs merging into the integration branch

```markdown
## Summary
- <what user-visible thing changes — bullet one>
- <bullet two, one more if the change genuinely spans two surfaces>

## Design choices
- **<Decision>**: <"X because Y" — one line; the rationale a reader can't recover from the diff>
- **<Decision>**: <...>

## Concepts introduced
<One short paragraph naming any new abstraction, contract, policy slot, or vocabulary the reader will encounter. Enough for a future agent scanning stacked PRs to say "ah, this is where FailureKind came from" without opening the diff. If the change is mechanical, write "None — mechanical change." and move on.>

## Plan
<the staged plan, pasted from ~/.claude/plans/toloka-tolokaforge/issue-<N>-<short-name>.md — plans have no in-repo home, so the PR body is the plan's durable record>

## Discovered issues
- Filed: #<n>, #<m>
- Fixed in this PR: <one-line each>

## Test plan
- <what to verify post-merge — one bullet per verification>

Closes #<issue>
```

**Anti-patterns to avoid:**
- Restating the diff. "Renamed X to Y" is what the diff shows; the PR body says *why*.
- Decision entries that are actually implementation notes ("used a `dict` for O(1) lookup"). Design choices are the branches taken at forks the reader would otherwise wonder about — data model shape, error semantics, migration story, whether a contract is a Protocol or a Pydantic model.
- Empty `Concepts introduced` for architectural changes. If the ticket introduced a new policy slot or a new task contract, name it — the milestone consolidation PR draws its `Key design choices` table from these sections and cannot cite what wasn't written down.

---

## 2. Consolidation PR body — for the single PR from `feat/<slug>` to `main`

The consolidation PR body **is** the finalized running design journal (`~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md`, a scratch file outside the repo). Copy the file contents verbatim, skipping only the file's own `# Milestone <N>: <title>` H1 — `gh pr create --title` supplies the PR title.

```markdown
## TL;DR
<One paragraph. Start with the compatibility posture — "This milestone changes X and does NOT change Y" — because that is the reviewer's first anxiety. Name the shape of the change (new subsystem, new contract, refactor with no behaviour change, …). End with the roll-up of every issue: "Closes #<n1>, #<n2>, #<n3>, ...">

## Impact on existing tasks — read this first
- **Today (nothing changes):** <what continues to work exactly as before, and where the guard rails are>
- **Near-term (opt-in):** <what users can adopt today if they want the new surface>
- **Longer-term (planned):** <where this milestone points; forward links to follow-up issues>

## Design walkthrough
<Two or three paragraphs framing the shape of the change. Enough that a reader who never saw the milestone can hold the picture. The Mermaid block below is the picture — not decorative.>

```mermaid
flowchart LR
    A[<node>] --> B[<node>]
    B --> C[<node>]
```
<Choose the diagram shape by fit: flowchart for architecture, sequence for interactions across services, state for lifecycle changes. The diagram is required for architectural milestones; omit only for milestones that are purely additive with no structural change.>

### <Subsystem or concept 1>
<Sub-section paragraphs when a single concept needs its own walkthrough. Prefer annotated code / YAML / message blocks over prose when the schema *is* the picture.>

### <Subsystem or concept 2>
<...>

## Key design choices

| Decision | Rationale |
|---|---|
| <Choice> | <"X because Y — and here is what we deliberately rejected"> |
| <Choice> | <one row per accepted decision, sourced from each per-issue PR's Design choices section> |

## Industry precedents
<Include only when the milestone was informed by prior art. For each precedent: a link, one sentence of what was borrowed, one sentence of what was deliberately rejected. Reversibility framing — "this choice can be revisited if X changes" — is welcome. Omit the section when N/A rather than filling it with hand-waves.>

- **<Project or paper>** ([link]) — Borrowed: <one line>. Rejected: <one line>.
- **<...>** — ...

## Suggested review order
The stacked PRs, in the order that makes them make sense:

1. **#<PR>** (`<squash-sha>`) — <one-line reason this is first>
2. **#<PR>** (`<squash-sha>`) — <one-line reason>
3. ...

## Verification
- CI lanes: <lint / unit / canonical / integration pass counts, matrix legs>
- Post-merge validations run: <targeted dev-MCP `run_tests`, docker stack health, capability tests>
- Deliberately skipped: <lane> — <reason, e.g. "integration-openai; no OPENAI_API_KEY set in this session — smoke covered by the equivalent Gemini lane">

## What's next
<Two sentences of forward-looking scope. Follow-up issues filed during the milestone are linked here (`#<n>`, `#<m>`) with a one-line description each. If a follow-up milestone is already tracked, name it.>
```

**Anti-patterns to avoid:**
- Reconstructing the body from squash commits at the end. Squash commit subjects are terse and lose the reasoning; the design journal is written as issues merge (see `/implement-milestone` Step 3.6) so the consolidation body is honest, not archaeological.
- Skipping the Mermaid diagram for architectural milestones because "the reader can figure it out". The diagram compresses what the prose has to spell out — its absence is what makes long PR bodies feel undifferentiated.
- Decorative `Industry precedents` sections with no rejected-precedents. If nothing was rejected, the section is a citation list, not design reasoning — better to omit and put links inline where relevant.
- Dropping the `Impact on existing tasks — read this first` section for a "small" milestone. Reviewer anxiety about compat impact is inversely proportional to how much this section reassures them; err on the side of writing it, even briefly.
- Leaving `Suggested review order` unstamped ("in numerical order"). The order that makes the story readable is often not the order of PR numbers.
- Any AI-attribution footer. Repo policy: no Claude/AI attribution in commits or PR bodies.
