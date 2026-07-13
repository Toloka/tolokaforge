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
/executing-development-tickets <issue-number>
```

or natural language: "execute issue #237", "let's do #244".

Examples:
- `/executing-development-tickets 237`
- "Execute issue #245"

The skill expects the issue to live in `Toloka/tolokaforge`. If a different repo is needed, ask the user.

## Workflow

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
   docs/FUTURE_DEVELOPMENT.md, and any related docs/plans/ entries.
2. Reproduce the current behaviour by running it: dev MCP run_tests / run_python,
   `make docker-up` for env services, a targeted `tolokaforge run` if the behaviour
   only shows end-to-end. For a bugfix, capture the reproducing failure.
3. File any out-of-scope "Discovered issues" via the GitHub MCP and reference the
   numbers in the plan.
4. Write the plan to docs/plans/<YYYY-MM-DD>-issue-<N>-<short-name>.md.
5. Return the structured "Handoff to main" block per your spec. Stop there —
   do not create the branch, do not launch other agents, do not open a PR.

Honour your binding principles: interfaces over implementation, compatibility
surfaces need explicit migration (internals refactor cleanly), diagnose by
running, lock behaviour with tests at the right tier, no compromise, surface
discovered issues, comment hygiene.
```

### Step 4 — Critique loop (architect ↔ critic)

If the architect returned `DISCOVERY-BLOCKER: ...` instead of a plan, relay it to the user and stop. No critique, no branch, no execution.

Otherwise, before the user sees the plan, pressure-test it:

1. **Launch** `plan-critic` via the Agent tool with `name=critic` (so re-critique rounds go through `SendMessage` and keep its context). Prompt:
   ```
   Critique the plan at <docs/plans/...> for issue #<N> in Toloka/tolokaforge (round 1).

   Issue body:
   <verbatim issue body>

   Architect's handoff:
   <verbatim handoff block>

   Apply your critique dimensions. Verify the plan's claims against the repo and
   (read-only) the running behaviour. Return your structured verdict block. Do not
   edit the plan — the architect owns it.
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
git checkout main
git pull origin main
git checkout -b <branch-from-plan>
```

Confirm clean working tree first (`git status`). If dirty, ask the user before proceeding.

### Step 7 — Stage dispatch loop

For each stage in the plan, serially (never in parallel — the working tree and commit history are shared):

1. **Launch** `plan-stage-implementer` via the Agent tool, fresh context per stage. Prompt:
   ```
   Implement Stage <N> of <docs/plans/...>. Full plan path: <docs/plans/...>.
   Stage block (verbatim):

   <paste stage block from plan>

   Contract: the stage is not done until the behaviour-locking test exists at the
   tier the stage names (unit / canonical / integration), exercises real behaviour
   (not mocks), and passes. One stage = one commit. Return your structured report.
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

### Step 8 — Branch review

When all stages are done, launch `branch-code-reviewer` via the Agent tool. Prompt:

```
Review the current branch vs main against AGENTS.md and the code-review skill rules.
Plan: <docs/plans/...>. Cover branch + staged + unstaged. Return findings in the
standard format.
```

### Step 9 — Fix Blocker / Major findings

If the reviewer returns 🔴 Blocker or 🟠 Major findings:

1. Group findings by file / theme.
2. Launch `plan-stage-implementer` with prompt:
   ```
   Apply these reviewer corrections to the branch. Each finding has file:line and a
   suggested fix. Update or add behaviour-locking tests if behaviour changes. One
   commit. Return your structured report.

   <paste reviewer findings verbatim>
   ```
3. Re-launch `branch-code-reviewer` on the updated branch.
4. Loop until verdict is `APPROVE` or `APPROVE WITH NITS`. **Cap: 2 fix rounds.** If the reviewer keeps finding the same Blocker, stop and ask the user — don't grind the implementer.

Nit / Minor findings: surface to the user as optional follow-ups; don't block the PR on them.

### Step 10 — Open the PR

```bash
gh pr create --title "<concise title from plan>" --body "$(cat <<'EOF'
## Summary
<2–3 bullets from the plan's Goal>

## Plan
See docs/plans/<file>

## Discovered issues
- Filed: #<n>, #<m>
- Fixed in this PR: <one-line each>

## Test plan
- <bullets — what to verify post-merge>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The `claude-review.yml` workflow will run its own hygiene pass on the PR — treat its findings like reviewer findings (fix or rebut, don't ignore).

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

## Anti-patterns

- **Don't relay the architect's plan unread.** Read the plan file yourself before showing it to the user. Spot obvious gaps (missing stage, missing test, vague contract) and push back via SendMessage *before* the user sees it.
- **Don't skip the critique loop.** One critic round on a clean plan is cheap; an ungrounded plan discovered at stage 3 is not. The only skip is `DISCOVERY-BLOCKER` (nothing to critique).
- **Don't let the critique loop grind.** 3 rounds max, findings must be fixed or rebutted, and the critic doesn't get to redesign. Unresolved after the cap → the user arbitrates.
- **Don't let workers orchestrate.** If the architect spawns another agent or starts mutating the repo, that's a spec violation — call it out. Same for the implementer launching a reviewer, or the critic editing the plan.
- **Don't dispatch stages in parallel.** Working tree is shared; commit ordering matters.
- **Don't skip Step 5's approval.** The plan needs the user's eyes before main starts mutating the repo — the critic's verdict doesn't replace them.
- **Don't run this skill for `docs:` / `chore:` issues** that don't need staged implementation. Just edit and commit directly.
- **Don't dispatch this skill for issues already in progress on a branch.** Continue manually or ask the user.
