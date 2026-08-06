---
name: implement-milestone
description: >
  Drive a whole GitHub milestone end-to-end on a dedicated integration branch:
  sequence issues, run the executing-development-tickets pipeline per issue with
  a relaxed approval gate, squash-merge PRs into the integration branch on green
  CI, keep issues/umbrella/milestone current, validate every merge, file
  prioritized follow-ups, and — once all issues are closed — prepare a single
  consolidation PR from the integration branch to `main` with a rich design
  writeup, then stop and hand off to the user for review and merge.
  Triggers on: "implement milestone N", "/implement-milestone <N|URL>",
  "execute milestone N", "finish the milestone", "run milestone #8".
---

# Implementing a Milestone

Main-process orchestrator one level above `/executing-development-tickets`. One milestone = many issues = many per-issue PRs stacked on a single **integration branch** `feat/<milestone-slug>`, driven serially to merged into that branch without stopping between issues. When every issue is closed, the skill opens **one consolidation PR** from `feat/<milestone-slug>` to `main` — with a design walkthrough, decision/rationale table, and a Mermaid diagram — and stops there for human review. **Main never implements issues inline** — every issue goes through the executing-development-tickets pipeline (architect → critic → stage implementers → reviewer); this skill owns the loop around it: sequencing, CI, merging into the integration branch, bookkeeping, post-merge validation, escalation, and the final consolidation PR.

## Invocation

```
/implement-milestone <milestone-number-or-URL>
```

or natural language: "implement milestone 8", "finish milestone https://github.com/Toloka/tolokaforge/milestone/8".

## Standing permissions

Invoking this skill **is** the user's explicit grant to, without re-asking:

- Run the `/executing-development-tickets` pipeline per issue, including all its subagents.
- Create, update, label, re-scope, and close GitHub issues; file follow-up issues; assign issues to the milestone; comment on issues and the umbrella.
- Create the milestone **integration branch** `feat/<milestone-slug>` off `main`, push it, and force-push it during in-milestone rebases against `main` (no one else pushes to a milestone integration branch).
- Create per-issue feature branches off the integration branch; commit; push; rebase feature branches on the integration branch.
- Open per-issue PRs against the integration branch and **squash-merge them once CI is green** (`gh pr merge <N> --squash`).
- Start, stop, and rebuild the local Docker stack (`make docker-up` / `docker-down` / `docker-build`) as needed for validation.
- Rebase the integration branch on `main` at the end of the milestone and **open** the consolidation PR from the integration branch to `main`.
- Close the milestone when the user has merged the consolidation PR.

**Never granted — always stop and ask:** releases and version tags (`release.yml` / `release-gate.yml` are human-triggered), publishing packages, anything touching provider accounts or API-key budgets beyond normal test runs, force-pushing or committing directly to `main`, deleting the milestone, merging on red or unverified-flaky CI, and — critically — **merging the consolidation PR into `main`**. The consolidation PR is prepared but never auto-merged; the human reviews and merges it.

## Step 0 — Progress channel

Every per-issue sub-skill run writes its subagent events into a milestone-scoped tree; the milestone loop tails a rollup file so cross-issue activity is visible in one stream.

1. Create the milestone events file (touching first guarantees the tail has a real file to follow):
   ```bash
   mkdir -p ~/.claude/plans/toloka-tolokaforge/progress/milestone-<N>/
   : > ~/.claude/plans/toloka-tolokaforge/progress/milestone-<N>/rollup.jsonl
   ```
2. Start the tail as a **single-file** background bash and subscribe with `Monitor`:
   ```bash
   tail -n0 -F ~/.claude/plans/toloka-tolokaforge/progress/milestone-<N>/rollup.jsonl
   ```
   Use `run_in_background: true`. No glob, no directory watch.
3. For each per-issue sub-skill invocation in Step 3.2, pass `progress_root=~/.claude/plans/toloka-tolokaforge/progress/milestone-<N>/issue-<K>/`. The sub-skill creates its own `events.jsonl` under that directory and threads `PROGRESS_FILE=<progress_root>/events.jsonl` into each subagent launch.
4. After the sub-skill completes for issue K, main appends the issue's event lines into the milestone rollup so the milestone tail surfaces them: `cat <progress_root>/events.jsonl >> ~/.claude/plans/toloka-tolokaforge/progress/milestone-<N>/rollup.jsonl`. Per-issue files are kept in place as the authoritative per-issue record; the rollup is a stream convenience.

Progress files are scratch, never committed. Schema, write recipe, and per-agent phase lists live in `/executing-development-tickets` §Progress protocol.

## Step 1 — Intake

1. Resolve the milestone: `gh api 'repos/Toloka/tolokaforge/milestones/<N>'` (the URL's trailing number is the milestone number). List its issues, open and closed: `gh api 'repos/Toloka/tolokaforge/issues?milestone=<N>&state=all&per_page=100'`.
2. Read the **umbrella issue** in full — it is the SSOT for scope, sequencing constraints, and completion state. Read any design docs it links (`docs/*.md`). Read other issues only as needed for clarity.
3. Build the execution queue. Ordering: hard dependencies first, then priority, then risk — pull the keystone / highest-uncertainty issue early so design problems surface while everything is still cheap to change.
4. Derive `<milestone-slug>`: kebab-case of the milestone title, ≤ 30 chars, alnum + `-` only. Examples: milestone "Terminal DX" → `terminal-dx`; "Multi-container environments" → `multi-container`.
5. Post the queue **and the chosen slug** to the user as a status message (ordering + anything that looks stale or mis-premised + the integration-branch name `feat/<slug>`) and **proceed** — this is informational, not an approval gate. The user interrupts if they disagree with the ordering or the slug.

## Step 2 — Integration branch

1. `git fetch origin`. Working tree must be clean; if dirty, ask before mutating.
2. If `origin/feat/<slug>` **exists**:
   - Adopt it: `git checkout feat/<slug> && git pull --ff-only origin feat/<slug>`.
   - Compare it to `origin/main`: `git log --oneline origin/main..feat/<slug>` — the commits should look like prior per-issue merges of this milestone. If it looks like an unrelated branch, stop and ask.
   - Look for `~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md` — if present, adopt it as the running design journal. If missing, create it (Step 2.4) from the umbrella and existing squash-merge commit messages.
3. If `origin/feat/<slug>` **does not exist**:
   - `git checkout main && git pull origin main`.
   - `git checkout -b feat/<slug>`.
   - `git push -u origin feat/<slug>`.
4. **Bootstrap the running design journal.** Create `~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md` (a scratch file outside the repo — never committed) with:
   - `# Milestone <N>: <title>` header + link to the milestone + link to the umbrella issue.
   - Empty stubs for the final-PR sections: `## TL;DR`, `## Impact on existing tasks`, `## Design walkthrough` (with a placeholder ```mermaid``` block), `## Key design choices` (empty Decision / Rationale table with header row), `## Industry precedents`, `## Suggested review order`, `## Verification`, `## What's next`.
   - Write this seed to the scratch file — do NOT commit it. No commits land directly on the integration branch: every change arrives via a squash-merged per-issue PR, and the final rebase against `main` in Step 4 adds none.
5. **Warm the Docker stack once.** Run `make docker-up` and confirm `make docker-status` reports the core services healthy. Keep the stack warm across the milestone — do not tear it down between issues. Tear down only on error (see Failure modes) or after the consolidation PR is open. Per-issue Docker cold-starts add 30–60 s each; keeping the stack warm across the milestone removes them from every issue after the first.

## Step 3 — Per-issue loop

For each issue, serially (one branch / one PR in flight at a time — the working tree is shared and the per-issue pipeline is already multi-agent):

1. **Sync:** `git checkout feat/<slug> && git pull --ff-only origin feat/<slug>`. Working tree must be clean.
2. **Run the executing-development-tickets pipeline** with `base_branch=feat/<slug>` — per-issue branches branch off the integration branch and per-issue PRs target it. The pipeline drives through PR creation, with the milestone-mode deltas below.
3. **CI + next-issue architect prep, concurrent.** The CI wait is the largest recoverable idle window in the milestone loop; use it to prepare the next issue's plan while main polls GitHub.
   1. Start `gh pr checks <PR#> --watch` as a **background Bash process** (`run_in_background: true`). Capture its stream via the Monitor tool — a task-notification fires when CI resolves; do not poll.
   2. **If there is a next issue N+1 in the queue** (guard: skip on the last issue in the milestone): immediately dispatch `system-architect-planner` for N+1 via the Agent tool with `name=architect-next`. Its prompt tells it to plan against `origin/feat/<slug>` at the current tip (which will match `feat/<slug>` post-N-merge to within the squash-commit boundary), read the plan file for issue N (`~/.claude/plans/toloka-tolokaforge/issue-<N>-<short-name>.md`) as landing-surface context, and use the briefing pack per `/executing-development-tickets` §Step 3b. Architect is hard-limited read-only — no code edits, no git mutations — so it cannot disturb the shared working tree.
   3. Main can, in parallel with both, scaffold the umbrella-comment and design-journal-append templates for issue N (Steps 3.5–3.6) — these do not depend on CI outcome.
   4. **When CI resolves:** on failure, decide *branch-caused vs pre-existing* — check whether the same lane fails on `feat/<slug>` before grinding — and, if that lane also fails on `main`, it's genuinely upstream. Branch-caused → corrective `plan-stage-implementer` launch (kill architect-next first if the correction will reshape the landing surface). Pre-existing → note it on the PR, file an issue if unfiled, and don't block on it if the branch's own lanes are green. (Known pre-existing formatting drift is documented in AGENTS.md gotchas #3/#4 — don't mistake it for branch damage.)
4. **Merge when green:** confirm the PR body says `Closes #<issue>`, then `gh pr merge <PR#> --squash` (target: the integration branch). Verify the issue auto-closed; close it manually with a one-line evidence comment if not.
5. **Bookkeeping:** tick the umbrella checklist; comment on the umbrella: `#<issue> → PR #<pr> → <merge-sha> — <one-line outcome; follow-ups filed: #a, #b>`. This comment is load-bearing (see Durable state).
6. **Append to the design journal.** In `~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md`, append: (a) a one-paragraph "what this issue delivered" note, (b) 1–3 rows for the Decision / Rationale table drawn from the per-issue PR's "Design choices" section, and (c) any new "Concepts introduced" one-liner. Write to the scratch file only — do not commit it anywhere. The umbrella comment (Step 3.5) is the durable per-issue record.
7. **Post-merge validation, concurrent with any still-running architect-next.** `git checkout feat/<slug> && git pull`, then validate the shipped behaviour — dev MCP `run_tests` targeted at the affected markers/paths; `make docker-status` (the stack is warm from Step 2.5 — only `make docker-up` again if it reports unhealthy); for LLM-layer or model changes a targeted capability test, or a cheap `tolokaforge run` smoke against a bundled example (needs LLM keys in `.env`, costs real tokens — keep it minimal). A broken integration branch is never acceptable: fix it before starting the next issue.

   If architect-next has already returned, main can begin the critic step for N+1 (per-issue pipeline Step 4) while validation runs. Both are read-only against the working tree — validation reads `feat/<slug>`, critic reads the plan file — so overlap is safe.
8. **Next issue.** When issue N+1 begins, main is in one of two states:
   - Already holding architect-next's plan (returned during Step 3.3 or Step 3.7) — skip per-issue pipeline Steps 1 (fetch issue) and 3 (launch architect); proceed to Step 4 (critique).
   - Still waiting on architect-next (rare after a full CI wait) — use the wait to run the critic on whatever architect-next has produced so far, or fall back to the original serial pipeline order if architect-next has not yet returned a plan handoff.

### Milestone-mode deltas to executing-development-tickets

Everything in that skill applies except:

- **Base branch: pass `base_branch=feat/<slug>`.** The pipeline's Step 6 (branch creation) branches off it; Step 10 (PR creation) targets it via `gh pr create --base feat/<slug>`.
- **Step 2 (confirm target): don't ask the user.** Validate the issue premise against the umbrella and the current behaviour instead. Ticket premises rot — when evidence contradicts one, update or close the issue with the evidence attached and move on. Escalate only if the correction materially changes milestone scope.
- **Step 5 (plan approval): the critic loop is the gate.** Auto-approve when `plan-critic` returns `APPROVE` / `APPROVE WITH NOTES` and your own read of the plan is clean. Escalate to the user only for: critic↔architect deadlock; `DISCOVERY-BLOCKER` that evidence can't resolve; plans that break a compatibility surface (task contracts, config schemas, CLI, published API) or have irreversible data effects.
- **Step 11 (hand-off): don't stop at the per-issue PR.** Drive CI → merge into `feat/<slug>` → bookkeeping → journal update → post-merge validation per the loop above.

## Step 4 — Prepare the consolidation PR

Runs once the milestone has zero open issues (and every closed-as-completed issue has a `#<issue> →` line on the umbrella).

1. **Rebase the integration branch on `main`.** `git checkout feat/<slug> && git fetch origin && git rebase origin/main`. Squash-merged per-issue commits stay individual on the integration branch — the rebase only shifts them onto the current `main` tip so the final PR presents as linear history. If the rebase conflicts, resolve inside `feat/<slug>` (never on `main`); push with `--force-with-lease` since only this skill writes to the integration branch. If conflicts are non-trivial, stop and ask.
2. **Finalize the design journal.** Complete every stub in `~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md`:
   - **TL;DR** — one paragraph naming the compatibility impact (or lack thereof) and ending with the roll-up of every `Closes #<n>` in the milestone.
   - **Impact on existing tasks — read this first** — reviewer-safety framing: near-term / today / longer-term commitments, safety guards, follow-up ticket links.
   - **Design walkthrough** — required ```mermaid``` block. Pick the shape that fits (flowchart for architecture, sequence for interactions, state for lifecycles). The diagram is the picture of the change; do not omit it for architectural milestones.
   - **Key design choices** — Decision / Rationale markdown table, one row per accepted decision, sourced from the appends made in Step 3.6.
   - **Industry precedents** — when the milestone was informed by prior art, link each and state what was borrowed and what was deliberately rejected. Omit the section when N/A.
   - **Suggested review order** — numbered list of the per-issue PRs (with squash SHAs), ordered to make the story readable start-to-finish.
   - **Verification** — CI lane pass counts, post-merge validations that ran, and any lanes deliberately skipped with a reason.
   - **What's next** — one or two sentences of forward links to follow-up issues or the next milestone.
   - Finalize the scratch file in place — do not commit it.
3. **Draft the PR body.** The consolidation PR body **is** the design journal — copy the file contents verbatim into the PR body (skipping only the file's `# Milestone <N>:` header line, since `gh pr create --title` supplies the PR title). Match PR-121-style discipline: flat outline (`##` / `###` only), no emoji-heavy section names, no AI attribution footer.
4. **Open the PR.**
   ```bash
   gh pr create \
     --base main \
     --head feat/<slug> \
     --title "feat(<scope>): <milestone title>" \
     --body-file ~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md
   ```
   The `<scope>` follows the repo's conventional-commits convention — the subsystem the milestone predominantly touches (e.g. `runtime`, `cli`, `core`).
5. **Hand off and stop.** Post to the user: PR URL, one-line summary, follow-ups filed with priorities (implemented vs deferred), post-merge validations run, and the reminder that human review is the gate here. **Do not merge.** **Do not close the milestone yet** — that happens after the user merges the PR, on the user's cue.

The full template with placeholders and worked-example headings lives in `pr-templates.md` next to this file.

## Follow-ups and discovered work

File everything you discover (bugs, improvements, deferred edge cases) as issues per the `writing-development-tickets` conventions — type label always, priority stated in the body. Then triage:

- **High-priority and in-scope** → assign to the milestone and insert into the queue (implement this session).
- **Low-priority or out-of-scope** → file, cross-link from the source issue/PR, leave for later. Note it in the final report and in the design journal's `## What's next` section.

Never let discovered work die in a PR comment or the conversation — if it isn't an issue, it didn't happen.

## Durable state — the session will outlive its context

A milestone run is long; the conversation will be compacted. The durable record lives in GitHub and on the integration branch, never in the conversation:

- **Umbrella comments** are the run journal for merge events (Step 3.5) — one per merged issue.
- **`~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md`** — a scratch file outside the repo, persistent across sessions — is the running design journal (Step 2.4 and Step 3.6). It accumulates as issues merge and *becomes* the consolidation PR body in Step 4; it is never committed.
- **Issue state** (open/closed/labels/milestone) is always current — stale bookkeeping is a bug, not cosmetics.
- **Per-issue plans** live in `~/.claude/plans/toloka-tolokaforge/` (scratch, outside the repo); their durable record is the per-issue PR body that embeds them.

After compaction or an interrupted session, rebuild state from: the milestone page + umbrella comments + `git log feat/<slug>` + the running design journal + `gh pr list --base feat/<slug>` — never from what you remember of the conversation.

## Escalation — the only reasons to stop and ask

1. A compatibility-surface break, security-sensitive change, or irreversible data operation where the plan forces a choice.
2. A genuine design fork the umbrella and design docs are silent on, where the options materially diverge.
3. `main` or CI infrastructure broken in a way you didn't cause and can't fix, blocking all progress.
4. The integration branch `feat/<slug>` conflicts irreconcilably with `main` mid-milestone (Step 4 rebase surfaces conflicts that touch more than mechanical import ordering).
5. Milestone scope change: closing a major issue as wrong-premise, or discovered work large enough to rival an existing issue.
6. Consolidation PR review conflict: the human reviewer surfaces a design fork the skill missed. Stop, ask — do not attempt to unwind squash-merged issues.

Everything else has a sensible default: take it, record the decision in the issue/PR and the design journal, keep moving.

## Completion

Two phases:

**Phase A — skill-owned.** Every milestone issue and in-scope follow-up is closed. The integration branch is rebased on `main`. The consolidation PR is open with the finalized design journal as its body. Report to the user: table of issue → PR → merge commit; follow-ups filed with priorities (implemented vs deferred); premise corrections made; post-merge validations run; the consolidation PR URL. **Stop.**

**Phase B — human-owned.** The user reviews the consolidation PR and merges it (or requests changes). When the user confirms merge, close the milestone (`gh api -X PATCH 'repos/Toloka/tolokaforge/milestones/<N>' -f state=closed`). Anything else — release tags, provider accounts, package publishing — remains outside this skill's scope.

## Failure modes

| Symptom | Resolution |
|---|---|
| CI lane fails on the per-issue branch and on `feat/<slug>` | Pre-existing on the integration branch — file/annotate an issue, don't grind the branch. Merge only if the per-issue PR's own lanes are green. |
| CI lane fails on `feat/<slug>` and also on `main` | Genuinely upstream — file/annotate an issue against `main`; not a milestone blocker. |
| CI flake suspected | One re-run, max. Still red without a `feat/<slug>` or `main` repro → treat as branch-caused. |
| Merge conflict on a per-issue PR | Rebase the feature branch on `feat/<slug>`, re-run targeted tests, push. |
| Integration branch conflicts with `main` mid-milestone | Rebase `feat/<slug>` on `main`, `git push --force-with-lease`, re-run targeted tests. Only this skill writes to `feat/<slug>`, so the force-push is safe. |
| Docker stack broken after a merge | `make docker-down` + `make docker-build-core` + `make docker-up`; check `make docker-status`. Do not start the next issue on a broken stack. |
| Docker services drift (accumulated state, cached data) across issues | `make docker-down` + `make docker-up` to reset; verify `make docker-status` before the next issue's post-merge validation. State drift is not a correctness issue — behaviour-locking tests still exercise real behaviour — but reset if flakes appear. |
| Integration tests skip-green for missing keys | Check which keys the lane needs (AGENTS.md lists them); a skipped test is not a passed test when the skipped behaviour is what the issue shipped. |
| Issue already has an open PR | Yours (this session / a prior run, targeting `feat/<slug>`): adopt and drive it to merge. Someone else's, or targeting `main`: escalate. |
| Issue premise contradicted by evidence | Update or close it with the evidence, comment on the umbrella, continue. Escalate only on material scope change. |
| Context compacted mid-issue | Rebuild from umbrella comments + `~/.claude/plans/toloka-tolokaforge/milestone-<N>-integration.md` + `git log feat/<slug>` + `gh pr list --base feat/<slug>`. |
| `origin/feat/<slug>` exists but points to an unrelated branch | Ask the user before adopting or renaming — never overwrite unknown history. |
| The design-journal file would leak internal names (private repo names, private adapter/lib names, internal ticket IDs) into the consolidation PR body | Blocker — strip those refs before opening the PR. Public repo hygiene applies. |

## Anti-patterns

- **Don't implement issues inline on the integration branch.** Even a "one-liner" issue goes through the pipeline — inline fixes skip the critic, the reviewer, and the behaviour-locking test. No commits land directly on `feat/<slug>`: every change arrives via a squash-merged per-issue PR.
- **Don't batch unrelated issues into one per-issue PR.** 1 issue = 1 PR. Bundle only true duplicates, recorded in both issues.
- **Don't merge on red**, or on "probably flaky" without a `feat/<slug>` or `main` comparison.
- **Don't skip post-merge validation because CI is green.** CI lanes don't cover everything (integration lanes auto-skip without keys).
- **Don't leave bookkeeping for the end.** Umbrella comments, issue state, and the design journal are updated per-issue, not in a final sweep — compaction can hit at any time, and the journal *is* the future PR body.
- **Don't merge the consolidation PR yourself.** Preparing it is the skill's job; merging it is the human's. This is the deliberate quality gate — the whole point of the integration-branch flow is that a human reviews one well-documented PR instead of N stacked minimal ones.
- **Don't reconstruct the design journal after the fact.** Squash-commit messages are terse and lose the reasoning; the journal is written as you go so the consolidation PR body is honest, not archaeological.
- **Don't re-ask for permissions this skill grants**, and don't assume ones it excludes.
- **Don't parallelize issues.** Shared working tree; serial merges keep every rebase trivial. The one sanctioned overlap is architect-next (read-only plan prep for issue N+1) running during CI wait for issue N — everything else stays serial.
- **Don't launch architect-next after the last remaining issue in the milestone.** Nothing to plan against — the milestone is done. Guard the launch on `remaining_issues > 1`.
- **Don't let architect-next proceed silently against stale state.** If issue N's corrective fix reshapes the landing surface (adds/removes contracts, renames modules architect-next was targeting), kill architect-next and re-launch after the fix is merged. CI-wait overlap is a wall-clock optimization, not a correctness license.
- **Don't leak internal names into public artefacts.** Consolidation PR bodies, per-issue PR bodies, and journal files land on `main` (a public repo) — no private repo names, no private adapter/lib names, no internal ticket IDs. Link from the internal side instead.
