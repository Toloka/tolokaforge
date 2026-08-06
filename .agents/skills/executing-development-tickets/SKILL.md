---
name: executing-development-tickets
description: >
  Drive a GitHub issue end-to-end. Main orchestrates: architect plans, critic
  pressure-tests the plan, stage implementers execute, reviewer reviews, main opens
  the PR. Single entry point.
  Triggers on: "execute issue #N", "implement issue N", "/executing-development-tickets <N>",
  "let's do #N", "build issue N".
---

# Executing Development Tickets

Main-process orchestrator that takes a GitHub issue and drives it to a reviewed PR. **Main owns orchestration.** Subagents are workers, not orchestrators — they do their narrow job and return.

Four workers, each launched by main:
1. `system-architect-planner` — studies the repo + current behaviour, writes a staged plan, returns it. Does NOT execute it.
2. `plan-critic` — adversarially critiques the plan before the user sees it, returns a verdict. Does NOT edit the plan.
3. `plan-stage-implementer` — implements one stage in a fresh context, returns a structured report.
4. `branch-code-reviewer` — reviews the whole branch against `AGENTS.md` rules, returns findings.

Main is responsible for: target confirmation, critique-loop mediation, plan approval gate, branch creation, stage dispatch loop, drift handling, reviewer dispatch, fix dispatch, PR opening.

## Invocation

```
/executing-development-tickets <issue-number> [base_branch=<ref>]
```

or natural language: "execute issue #237", "let's do #244".

Examples:
- `/executing-development-tickets 237`
- "Execute issue #245"
- `/executing-development-tickets 245 base_branch=feat/terminal-dx` — target a milestone integration branch

The skill expects the issue to live in `Toloka/tolokaforge`. If a different repo is needed, ask the user.

**Base branch.** Optional context, defaults to `main`. Direct invocations should almost always use the default. `/implement-milestone` passes `base_branch=feat/<milestone-slug>` so per-issue branches stack on the milestone integration branch and per-issue PRs merge into it rather than `main`. When set, it replaces `main` in Step 6 (branch creation) and Step 10 (PR creation). Nothing else changes — the critic, implementer, and reviewer are agnostic to the base branch.

## Workflow

### Step 0 — Progress channel

Before Step 1, set up a JSONL progress channel so main can observe subagent activity in real time. Every subagent this skill launches writes phase-transition lines to a well-known file; main tails that stream via a background bash + `Monitor`.

1. Pick a `launch_id` root — `issue-<N>` for a numbered issue, or `adhoc-<UTC-timestamp>` if invoked without an issue number.
2. Create the progress directory: `mkdir -p ~/.claude/plans/toloka-tolokaforge/progress/<launch_id>/`.
3. Start the tail as a background bash — `tail -F ~/.claude/plans/toloka-tolokaforge/progress/<launch_id>/*.jsonl 2>/dev/null` — with `run_in_background: true`, and subscribe to it via the `Monitor` tool. Each JSONL line becomes a notification; main can read the stream without polling.
4. Pass the concrete file path to every subagent as `PROGRESS_FILE=~/.claude/plans/toloka-tolokaforge/progress/<launch_id>/<agent-role>-<UTC-timestamp>.jsonl` in the launch prompt (see per-step prompts below).

Progress files are scratch, never committed. They live outside the repo per the same convention as plan and briefing files. The full schema, per-agent phase lists, and reference examples are in the `## Progress protocol` section at the end of this file — read it once, then apply per subagent.

Watchdog behaviour (soft-idle nudge / hard-timeout escalation) is not part of this step yet; it lands in a follow-up change and reads the same stream. For now, the stream exists so main can *see* activity — that alone is enough to distinguish a working agent from a wedged one at a glance.

### Step 1 — Fetch the issue

Use GitHub MCP `issue_read` with `owner=Toloka`, `repo=tolokaforge`, `issue_number=<N>`. Capture title, body, labels.

If the issue is closed or already has a linked PR, stop and ask the user how to proceed.

### Step 2 — Confirm the target

Show the user a one-paragraph restatement of what the issue asks for and ask:

> "Is this the work you want me to plan and implement?"

Wait for "yes" / a correction / "ask the architect first". Don't proceed silently.

### Step 3 — Launch the architect (planning only)

Use the Agent tool with `subagent_type=system-architect-planner` and `name=architect` (so you can revise via `SendMessage`). Brief prompt:

```
Issue #<N> in Toloka/tolokaforge — <title>

Body:
<verbatim issue body>

Produce a staged plan only. Do NOT execute it — main will dispatch stages.

Drive your standard planning workflow:
1. Read root AGENTS.md, README.md, the docs/*.md for every subsystem touched,
   docs/FUTURE_DEVELOPMENT.md, and any related plans under
   ~/.claude/plans/toloka-tolokaforge/.
2. Reproduce the current behaviour by running it: dev MCP run_tests / run_python,
   `make docker-up` for env services, a targeted `tolokaforge run` if the behaviour
   only shows end-to-end. For a bugfix, capture the reproducing failure.
3. File any out-of-scope "Discovered issues" via the GitHub MCP and reference the
   numbers in the plan.
4. Write the plan to ~/.claude/plans/toloka-tolokaforge/issue-<N>-<short-name>.md
   (a scratch path outside the repo — plans are never committed to the tree; the
   plan's durable home is the per-issue PR body).
5. Return the structured "Handoff to main" block per your spec. Stop there —
   do not create the branch, do not launch other agents, do not open a PR.

Honour your binding principles: interfaces over implementation, compatibility
surfaces need explicit migration (internals refactor cleanly), diagnose by
running, lock behaviour with tests at the right tier, no compromise, surface
discovered issues, comment hygiene.

PROGRESS_FILE=<path built by main per Step 0, e.g. ~/.claude/plans/toloka-tolokaforge/progress/issue-<N>/system-architect-planner-<UTC-ts>.jsonl>
Append one JSONL line to $PROGRESS_FILE at each phase transition per your
charter's Progress reporting section.
```

### Step 3b — Assemble the per-issue briefing pack

After the architect returns, before dispatching the critic, main assembles a **briefing pack** — a scratch file at `~/.claude/plans/toloka-tolokaforge/issue-<N>-briefing.md` (never committed) that every subsequent subagent reads first. The pack cuts per-subagent cold-start cost by pre-selecting the `AGENTS.md` sections and `docs/*.md` slices relevant to this issue rather than each agent reading everything from cold.

Structure:

```markdown
# Briefing — issue #<N>: <title>

## Subsystems touched
<bullets — from architect's handoff or the plan's Stage list>

## AGENTS.md anchors relevant to this issue
- **Core Rules:** <N, M, ...> — the rule numbers this issue's stages implicate
- **Type-system table row:** <the row that governs any new contract this issue introduces>
- **Gotchas:** <#a, #b> — numbered gotcha entries relevant to the affected subsystems

## docs/*.md excerpts
<For each `docs/*.md` file the plan cites: file path + a short quoted excerpt of the section that matters. Aim for 3–10 lines per file, not the whole file.>

## Plan summary
<Copy the plan's Goal, Non-goals, and per-stage headings — enough that a subagent can hold the shape of the change without re-opening the plan.>

## Compatibility surfaces touched
<bullets — from the plan's per-stage "Compatibility" fields>

## Discovered-issue links
<Issue numbers filed by the architect during discovery, one line each.>
```

Every subsequent subagent's prompt (critic, stage implementers, reviewers, and revision-round SendMessages to the architect) starts with: *"Read `~/.claude/plans/toloka-tolokaforge/issue-<N>-briefing.md` first — it has AGENTS.md rules and docs pre-selected for this issue. Read source `AGENTS.md` and `docs/*.md` in full only when the briefing is silent on your question."*

The briefing is a **bootstrap, not a replacement**. AGENTS.md remains binding — the full-read fallback preserves rigour. Update the briefing's "Plan summary" section whenever the plan revises in Step 4; keep the file under ~5 KB (long briefings defeat the purpose).

### Step 4 — Critique loop (architect ↔ critic)

If the architect returned `DISCOVERY-BLOCKER: ...` instead of a plan, relay it to the user and stop. No critique, no branch, no execution.

Otherwise, before the user sees the plan, pressure-test it:

1. **Launch** `plan-critic` via the Agent tool with `name=critic` (so re-critique rounds go through `SendMessage` and keep its context). Prompt:
   ```
   Critique the plan at <~/.claude/plans/toloka-tolokaforge/...> for issue #<N> in Toloka/tolokaforge (round 1).

   Issue body:
   <verbatim issue body>

   Architect's handoff:
   <verbatim handoff block>

   Apply your critique dimensions. Verify the plan's claims against the repo and
   (read-only) the running behaviour. Return your structured verdict block. Do not
   edit the plan — the architect owns it.

   PROGRESS_FILE=<path built by main per Step 0>
   Append one JSONL line to $PROGRESS_FILE at each phase transition per your
   charter's Progress reporting section.
   ```
2. **Verdict `APPROVE` / `APPROVE WITH NOTES`** → proceed to Step 5, carrying any 🟡 notes into the user summary.
3. **Verdict `REVISE`** → `SendMessage` the findings verbatim to the architect. The architect revises the plan and returns per-finding dispositions (fixed / rebutted-with-evidence). `SendMessage` the revision summary + dispositions back to the critic for re-critique.
4. **Cap: 3 critique rounds.** On `DEADLOCK` or an unresolved 🔴 at the cap, proceed to Step 5 anyway and present the disagreement to the user — both positions, one paragraph each. Never bury it, never pick a side silently.
5. **Main enforces protocol, not substance.** Don't argue the findings yourself; make sure every finding is either fixed or explicitly rebutted, and that the critic isn't redesigning the plan (that's an overstep — push back).

### Step 5 — Relay the plan, get approval

When the critique loop settles:

1. **Read the plan file yourself.** You're about to execute it — don't relay blind. Look for obvious gaps: a vague contract, a missing behaviour-locking test, an unplanned break of a compatibility surface, a stage that mixes interface and implementation, doc updates not named per stage. If you spot a gap the critic missed, push back via `SendMessage` to the architect *before* showing the plan to the user.
2. **Surface to the user:** plan file path, stage count, one-paragraph summary, critic verdict + round count (plus any unresolved findings or accepted rebuttals worth knowing), discovery surprises, "Discovered issues" filed, risks. Ask: "Approve, or revise?"
3. **Revise** → `SendMessage` the architect with the user's feedback. Re-run the critic (one round) only if the revision materially changes contracts or stages. Loop until approved.
4. **Approve** → continue.

### Step 6 — Create the branch

```bash
git fetch origin
git checkout <base_branch>            # main by default; feat/<slug> under /implement-milestone
git pull --ff-only origin <base_branch>
git checkout -b <branch-from-plan>
```

Confirm clean working tree first (`git status`). If dirty, ask the user before proceeding. If `<base_branch>` doesn't exist on `origin`, stop and ask — a milestone integration branch that should exist but doesn't is a bookkeeping error, not something to paper over.

### Step 7 — Stage dispatch loop

For each stage in the plan, serially (never in parallel — the working tree and commit history are shared).

**Persistent-mode toggle.** If the plan has **≤ 4 stages**, launch the first stage's implementer with `name=impl-issue-<N>` and use `SendMessage` to dispatch stages 2..N to the same instance. The persistent implementer keeps `AGENTS.md`, subsystem docs, plan, briefing, and prior-stage context in-conversation, eliminating ~30 s of cold-start per subsequent stage.

If the plan has **> 4 stages**, launch each stage's implementer with a fresh context. The accumulated context in a persistent 5-stage-plus implementer would push against its useful window and risk cross-stage decision leak.

Every `SendMessage` in persistent mode carries the current plan file contents verbatim, plus the reminder: *"The plan file is authoritative — ignore any prior version you may remember from earlier in this conversation."* This defends against cross-stage drift when main updates the plan mid-issue (Step 7.3 justified-drift handling).

Corrective implementer launches (unjustified drift → corrective launch; Step 9 fix loop) always use **fresh context** regardless of the persistent-mode toggle — corrections need their own reasoning trail and must not be tempted to reconcile with prior decisions.

The "one stage = one commit" contract, the drift-handling rules, and the corrective-launch cap are unchanged.

1. **Launch** `plan-stage-implementer` via the Agent tool. For stage 1: fresh Agent launch (`name=impl-issue-<N>` if the plan has ≤ 4 stages, otherwise unnamed). For stages 2..N in persistent mode: `SendMessage` to `impl-issue-<N>` instead. Prompt (same for both transports):
   ```
   Implement Stage <N> of <~/.claude/plans/toloka-tolokaforge/...>. Full plan path: <~/.claude/plans/toloka-tolokaforge/...>.
   Stage block (verbatim):

   <paste stage block from plan>

   Contract: the stage is not done until the behaviour-locking test exists at the
   tier the stage names (unit / canonical / integration), exercises real behaviour
   (not mocks), and passes. One stage = one commit. Return your structured report.

   PROGRESS_FILE=<path built by main per Step 0>
   Append one JSONL line to $PROGRESS_FILE at each phase transition per your
   charter's Progress reporting section.
   ```
2. **Validate the report.** When it returns:
   - Contract matches the stage spec — flag drift in your next user message.
   - Behaviour-locking test exists: open the file, confirm it exercises real behaviour at the named tier (not mocks), and carries the right pytest marker.
   - Docs updated in the same commit — verify with `git show --stat HEAD`.
   - Verification commands ran cleanly (dev MCP `lint_check` + `format_check`, targeted `run_tests`).
   - "Discovered issues" surfaced. For each: fix-in-this-PR (note for next stage or follow-up) or `mcp__github__issue_write` to file it.
3. **Drift handling.**
   - **Justified drift** (the plan was wrong in a way the implementer caught): update the plan file yourself, show the diff to the user, continue.
   - **Unjustified drift** (implementer expanded scope, weakened a check, suppressed a lint, mocked something that should exercise real behaviour): launch a corrective `plan-stage-implementer` with explicit revert instructions. Don't accept the drift silently.
4. **Don't dispatch the next stage** until the current one is verified clean. If you can't verify, stop and ask the user.

Per-stage cycle cap: if a stage requires more than 2 corrective implementer launches, stop and surface to the user — the plan likely needs revision by the architect.

### Step 8 — Branch review (sharded, parallel)

When all stages are done, launch **three sharded reviewers in parallel** via the Agent tool. Send all three tool calls in one message so they run concurrently:

- `reviewer-correctness` — owns Blocker rules 1, 3, 5, 6, 7 + Correctness / Security / Performance dimensions (behaviour bugs, silent failures, test-tier honesty, secret access).
- `reviewer-hygiene` — owns Blocker rules 2, 4, 8, 9, 11 + Maintainability / Documentation dimensions (harness/task-pack boundary, compat-surface migration, comment hygiene, structure, doc freshness).
- `reviewer-type-fit` — owns Blocker rule 10 + Type-system fit / Task design / Dockerfile / MCP-usage dimensions (contract shape, task-pack anti-patterns, model/provider evidence).

Each reviewer gets the same prompt shape (`subagent_type` differs; scope narrows by charter):

```
Review the current branch vs <base_branch> against AGENTS.md and the code-review
skill rules within YOUR scope. Plan: <~/.claude/plans/toloka-tolokaforge/...>.
Briefing: <~/.claude/plans/toloka-tolokaforge/issue-<N>-briefing.md> if present.
Cover branch + staged + unstaged. Return findings in the standard format.

PROGRESS_FILE=<path built by main per Step 0, one per shard>
Append one JSONL line to $PROGRESS_FILE at each phase transition per your
charter's Progress reporting section.
```

When all three return:

1. Concatenate findings from the three lanes.
2. Dedupe by `(file:line, rule)` — the rare cross-lane overlap resolves to the reviewer whose lane owns the rule; drop the duplicate.
3. Sort by severity (🔴 → 🟠 → 🟡 → 🔵), then by file path.
4. Proceed to Step 9 with the merged list.

Total review wall-clock ≈ the slowest single lane, not the sum. On a small diff or a direct `/code-review` invocation, the monolithic `branch-code-reviewer` remains available as a single-agent fallback; the sharded path is the default inside this pipeline.

### Step 9 — Fix Blocker / Major findings

If the reviewer returns 🔴 Blocker or 🟠 Major findings:

1. Group findings by file / theme.
2. Launch `plan-stage-implementer` with prompt:
   ```
   Apply these reviewer corrections to the branch. Each finding has file:line and a
   suggested fix. Update or add behaviour-locking tests if behaviour changes. One
   commit. Return your structured report.

   <paste reviewer findings verbatim>

   PROGRESS_FILE=<path built by main per Step 0, fix-round scoped>
   Append one JSONL line to $PROGRESS_FILE at each phase transition per your
   charter's Progress reporting section.
   ```
3. Re-launch the three sharded reviewers (`reviewer-correctness`, `reviewer-hygiene`, `reviewer-type-fit`) in parallel on the updated branch, same as Step 8. Merge findings the same way.
4. Loop until every lane's verdict is `APPROVE` or `APPROVE WITH NITS`. **Cap: 2 fix rounds.** If any lane keeps finding the same Blocker, stop and ask the user — don't grind the implementer.

Nit / Minor findings: surface to the user as optional follow-ups; don't block the PR on them.

### Step 10 — Open the PR

Per-issue PR bodies are read by three audiences: the human reviewer, the next agent that plans work in this area, and the milestone consolidation PR (which draws its Decision / Rationale rows from every stacked PR). The template below gives each of them what they need without ballooning the body — every heading below is required; keep bullets tight.

```bash
gh pr create --base <base_branch> --title "<concise title from plan>" --body "$(cat <<'EOF'
## Summary
<2–3 bullets from the plan's Goal — what user-visible thing changes>

## Design choices
- **<Decision>**: <"X because Y" — one line each; 1–3 rows for a typical ticket, more only if the ticket genuinely introduced multiple decisions>

## Concepts introduced
<One short paragraph naming any new abstraction, contract, policy slot, or vocabulary the reader will encounter. If none, write "None — mechanical change."  This is the section the milestone consolidation PR cites when explaining the whole to a future reader or agent.>

## Plan
<the staged plan, pasted from ~/.claude/plans/toloka-tolokaforge/issue-<N>-<short-name>.md — plans have no in-repo home, so the PR body is the plan's durable record>

## Discovered issues
- Filed: #<n>, #<m>
- Fixed in this PR: <one-line each>

## Test plan
- <bullets — what to verify post-merge>

Closes #<issue>
EOF
)"
```

Do not add an AI-attribution footer. The `claude-review.yml` workflow will run its own hygiene pass on the PR — treat its findings like reviewer findings (fix or rebut, don't ignore).

Full worked template with placeholder examples lives at `.agents/skills/implement-milestone/pr-templates.md` — same source of truth used by the milestone consolidation PR.

### Step 11 — Hand-off

Surface to the user:
- PR URL.
- Plan file path.
- Any GitHub issues filed from "Discovered issues".
- One-line summary of what shipped.

## Boundaries

- **Main orchestrates.** Architect plans, doesn't execute. Critic critiques, doesn't redesign or edit the plan. Implementer implements one stage, doesn't loop. Reviewer reviews, doesn't fix. If a worker tries to overstep (architect creates a branch, critic edits the plan, implementer launches another agent), that's a bug — push back via SendMessage and report.
- **Main never writes production code itself.** All code edits go through `plan-stage-implementer`. Main edits the plan file, files GitHub issues, runs git plumbing.
- **One stage = one commit.** Implementer's contract; don't squash or amend after the fact.
- **Serial stages.** Working tree is shared.
- **Approval gate is non-negotiable.** Step 5 happens before Step 6. No branch creation before the user has seen and approved the plan. The critic's APPROVE is a quality gate, not a substitute for the user's. (This is also AGENTS.md's own protocol: plan → **confirm** → build → verify.)

## Failure modes

| Symptom | Resolution |
|---|---|
| GitHub MCP can't read the issue | Ask user for the issue number; if private repo / auth issue, ask user to re-auth GitHub MCP. |
| Architect returns `DISCOVERY-BLOCKER` | Relay to user, stop. Don't create a branch. |
| Critic and architect deadlock (same 🔴 after 3 rounds) | Present both positions to the user at Step 5; the user arbitrates. Don't run extra rounds. |
| Critic returns `REVISE` on every round with new findings each time | Goalpost-moving — remind the critic of its round protocol; if it persists, take the surviving 🔴/🟠 findings to the user and drop the rest. |
| Implementer needs env services that aren't up | `make docker-up` (core) or `tolokaforge docker up --profile full` (browser/RAG tasks); `make docker-status` to verify. |
| Integration tests skip for missing keys | Keys live in `.env` (read via `scripts/with_env.sh`). If the behaviour under test needs a key the user hasn't set, ask — don't let a silent skip stand in for a pass. |
| Plan crosses 5+ stages | Acceptable if warranted; surface the count to the user so they can choose to split the issue. |
| Implementer drifts from the plan | Justified → update the plan, show diff, continue. Unjustified → corrective implementer launch. Cap at 2 corrections per stage; then revise the plan with the architect. |
| Reviewer keeps finding the same Blocker | Stop after 2 fix rounds and ask the user. Don't loop indefinitely. |
| Working tree dirty when Step 6 starts | Ask the user before mutating — uncommitted work may be theirs. |
| `base_branch` passed but not present on `origin` | Stop and ask. A milestone integration branch that should exist but doesn't is a bookkeeping error upstream in `/implement-milestone`, not something to paper over by silently falling back to `main`. |

## Anti-patterns

- **Don't relay the architect's plan unread.** Read the plan file yourself before showing it to the user. Spot obvious gaps (missing stage, missing test, vague contract) and push back via SendMessage *before* the user sees it.
- **Don't skip the critique loop.** One critic round on a clean plan is cheap; an ungrounded plan discovered at stage 3 is not. The only skip is `DISCOVERY-BLOCKER` (nothing to critique).
- **Don't let the critique loop grind.** 3 rounds max, findings must be fixed or rebutted, and the critic doesn't get to redesign. Unresolved after the cap → the user arbitrates.
- **Don't let workers orchestrate.** If the architect spawns another agent or starts mutating the repo, that's a spec violation — call it out. Same for the implementer launching a reviewer, or the critic editing the plan.
- **Don't dispatch stages in parallel.** Working tree is shared; commit ordering matters.
- **Don't skip Step 5's approval.** The plan needs the user's eyes before main starts mutating the repo — the critic's verdict doesn't replace them.
- **Don't run this skill for `docs:` / `chore:` issues** that don't need staged implementation. Just edit and commit directly.
- **Don't dispatch this skill for issues already in progress on a branch.** Continue manually or ask the user.

## Progress protocol

Every subagent this skill launches writes JSONL progress lines to a per-launch file main opens in Step 0. Main tails the directory via a background `tail -F` + `Monitor`. The stream lets main *see* activity in real time without polling, and is the substrate the watchdog (soft-nudge / hard-timeout) will read in a follow-up change.

### Path convention

```
~/.claude/plans/toloka-tolokaforge/progress/<launch_id_root>/<agent-role>-<UTC-timestamp>.jsonl
```

- `<launch_id_root>` = `issue-<N>` for a numbered issue, `adhoc-<UTC-ts>` otherwise. Under `/implement-milestone` the root is `milestone-<N>/issue-<K>`.
- `<agent-role>` = `system-architect-planner`, `plan-critic`, `plan-stage-implementer`, `branch-code-reviewer`, `reviewer-correctness`, `reviewer-hygiene`, `reviewer-type-fit`.
- `<UTC-timestamp>` = `date -u +%Y%m%dT%H%M%SZ` at launch. Rerunning the same role (fix-loop rounds, corrective launches) yields a distinct file per launch — history is preserved.

Progress files are scratch, never committed, never referenced from PR bodies. They live outside the repo, alongside plan and briefing files.

### Schema

One JSON object per line, `≤ 300 bytes`, no PII:

```json
{"ts":"2026-08-06T12:34:56Z","agent":"plan-stage-implementer","launch_id":"impl-issue-237-s2","phase":"impl","step":"lint_check","detail":"pass","elapsed_s":42,"issue":237,"stage":2}
```

Required fields: `ts` (UTC ISO-8601), `agent` (role name), `launch_id` (main-assigned; see below), `phase` (from the per-agent phase list in the charter). Optional: `step`, `detail`, `elapsed_s`, `issue`, `stage`, `round`.

Main assigns `launch_id`: for a persistent implementer, `impl-issue-<N>[-s<stage>]`; for shard reviewers, `reviewer-<lane>-issue-<N>[-r<round>]`; for architect/critic, `architect-issue-<N>` / `critic-issue-<N>[-r<round>]`. The `launch_id` is the join key: every line for a single Agent-tool launch (or its SendMessage continuations) carries the same value.

### When subagents write

Per each agent's `## Progress reporting` section in its charter:

- On start.
- At each phase transition (phase list is role-specific).
- Before any tool call expected to exceed 60 s (`phase:"long_call"` + `step:"<tool>"`).
- On caught error.

### Main-side watcher

Main launches at Step 0, once per invocation:

```bash
tail -F ~/.claude/plans/toloka-tolokaforge/progress/<launch_id_root>/*.jsonl 2>/dev/null
```

as a background bash (`run_in_background: true`), subscribed via the `Monitor` tool. New JSONL lines arrive as notifications; main reads them opportunistically. Nothing needs to poll.

Watchdog behaviour (soft-idle nudge, hard-timeout `TaskStop`) is not part of this iteration — it lands in a follow-up change and consumes this same stream. For now the observability alone is the win: a wedged agent shows no new lines, and that gap is visible without waiting for a return that never comes.
