---
name: implement-milestone
description: >
  Drive a whole GitHub milestone end-to-end: sequence its issues, run the
  executing-development-tickets pipeline per issue with a relaxed approval gate,
  merge PRs on green CI, keep issues/umbrella/milestone current, validate every
  merge, file prioritized follow-ups, escalate only critical forks.
  Triggers on: "implement milestone N", "/implement-milestone <N|URL>",
  "execute milestone N", "finish the milestone", "run milestone #8".
---

# Implementing a Milestone

Main-process orchestrator one level above `/executing-development-tickets`. One milestone = many issues = many PRs, driven serially to merged without stopping between issues. **Main never implements issues inline** — every issue goes through the executing-development-tickets pipeline (architect → critic → stage implementers → reviewer); this skill owns the loop around it: sequencing, CI, merging, bookkeeping, post-merge validation, escalation.

## Invocation

```
/implement-milestone <milestone-number-or-URL>
```

or natural language: "implement milestone 8", "finish milestone https://github.com/Toloka/tolokaforge/milestone/8".

## Standing permissions

Invoking this skill **is** the user's explicit grant to, without re-asking:

- Run the `/executing-development-tickets` pipeline per issue, including all its subagents.
- Create, update, label, re-scope, and close GitHub issues; file follow-up issues; assign issues to the milestone; comment on issues and the umbrella.
- Create and switch branches; commit; push; rebase feature branches on `main`.
- Open PRs and **merge them once CI is green** (`gh pr merge <N> --squash`).
- Start, stop, and rebuild the local Docker stack (`make docker-up` / `docker-down` / `docker-build`) as needed for validation.
- Close the milestone when every issue in it is closed.

**Never granted — always stop and ask:** releases and version tags (`release.yml` / `release-gate.yml` are human-triggered), publishing packages, anything touching provider accounts or API-key budgets beyond normal test runs, force-pushing or committing directly to `main`, deleting the milestone, and merging on red or unverified-flaky CI.

## Step 1 — Intake

1. Resolve the milestone: `gh api 'repos/Toloka/tolokaforge/milestones/<N>'` (the URL's trailing number is the milestone number). List its issues, open and closed: `gh api 'repos/Toloka/tolokaforge/issues?milestone=<N>&state=all&per_page=100'`.
2. Read the **umbrella issue** in full — it is the SSOT for scope, sequencing constraints, and completion state. Read any design docs it links (`docs/*.md`, `docs/plans/*.md`). Read other issues only as needed for clarity.
3. Build the execution queue. Ordering: hard dependencies first, then priority, then risk — pull the keystone / highest-uncertainty issue early so design problems surface while everything is still cheap to change.
4. Post the queue to the user as a status message (ordering + anything that looks stale or mis-premised) and **proceed** — this is informational, not an approval gate. The user interrupts if they disagree.

## Step 2 — Per-issue loop

For each issue, serially (one branch / one PR in flight at a time — the working tree is shared and the per-issue pipeline is already multi-agent):

1. **Sync:** `git checkout main && git pull origin main`. Working tree must be clean.
2. **Run the executing-development-tickets pipeline** through PR creation, with the milestone-mode deltas below.
3. **CI:** watch with `gh pr checks <PR#> --watch`. On failure, decide *branch-caused vs pre-existing*: check whether the same lane fails on `main` before grinding. Branch-caused → corrective `plan-stage-implementer` launch. Pre-existing → note it on the PR, file an issue if unfiled, and don't block on it if the branch's own lanes are green. (Known pre-existing formatting drift is documented in AGENTS.md gotchas #3/#4 — don't mistake it for branch damage.)
4. **Merge when green:** confirm the PR body says `Closes #<issue>`, then `gh pr merge <PR#> --squash`. Verify the issue auto-closed; close it manually with a one-line evidence comment if not.
5. **Bookkeeping:** tick the umbrella checklist; comment on the umbrella: `#<issue> → PR #<pr> → <merge-sha> — <one-line outcome; follow-ups filed: #a, #b>`. This comment is load-bearing (see Durable state).
6. **Post-merge validation** for anything runtime-affecting: `git checkout main && git pull`, then validate the shipped behaviour — dev MCP `run_tests` targeted at the affected markers/paths; for env-service changes `make docker-up` + `make docker-status`; for LLM-layer or model changes a targeted capability test, or a cheap `tolokaforge run` smoke against a bundled example (needs LLM keys in `.env`, costs real tokens — keep it minimal). A broken `main` is never acceptable: fix it before starting the next issue.
7. Next issue.

### Milestone-mode deltas to executing-development-tickets

Everything in that skill applies except:

- **Step 2 (confirm target): don't ask the user.** Validate the issue premise against the umbrella and the current behaviour instead. Ticket premises rot — when evidence contradicts one, update or close the issue with the evidence attached and move on. Escalate only if the correction materially changes milestone scope.
- **Step 5 (plan approval): the critic loop is the gate.** Auto-approve when `plan-critic` returns `APPROVE` / `APPROVE WITH NOTES` and your own read of the plan is clean. Escalate to the user only for: critic↔architect deadlock; `DISCOVERY-BLOCKER` that evidence can't resolve; plans that break a compatibility surface (task contracts, config schemas, CLI, published API) or have irreversible data effects.
- **Step 11 (hand-off): don't stop at the PR.** Drive CI → merge → bookkeeping → post-merge validation per the loop above.

## Follow-ups and discovered work

File everything you discover (bugs, improvements, deferred edge cases) as issues per the `writing-development-tickets` conventions — type label always, priority stated in the body. Then triage:

- **High-priority and in-scope** → assign to the milestone and insert into the queue (implement this session).
- **Low-priority or out-of-scope** → file, cross-link from the source issue/PR, leave for later. Note it in the final report.

Never let discovered work die in a PR comment or the conversation — if it isn't an issue, it didn't happen.

## Durable state — the session will outlive its context

A milestone run is long; the conversation will be compacted. The durable record lives in GitHub, never in the conversation:

- Umbrella comments are the run journal (step 2.5) — one per merged issue.
- Issue state (open/closed/labels/milestone) is always current — stale bookkeeping is a bug, not cosmetics.
- Plans live in `docs/plans/` on `main` once merged.

After compaction or an interrupted session, rebuild state from the milestone page + umbrella comments + `git log` — never from what you remember of the conversation.

## Escalation — the only reasons to stop and ask

1. A compatibility-surface break, security-sensitive change, or irreversible data operation where the plan forces a choice.
2. A genuine design fork the umbrella and design docs are silent on, where the options materially diverge.
3. `main` or CI infrastructure broken in a way you didn't cause and can't fix, blocking all progress.
4. Milestone scope change: closing a major issue as wrong-premise, or discovered work large enough to rival an existing issue.

Everything else has a sensible default: take it, record the decision in the issue/PR, keep moving.

## Completion

1. Every milestone issue and in-scope follow-up is closed; the milestone shows 0 open → close the milestone.
2. Final report to the user: table of issue → PR → merge commit; follow-ups filed with priorities (implemented vs deferred); premise corrections made; what was validated post-merge; and what remains for the user — typically release tagging and anything provider-account-related, which this skill never performs.

## Failure modes

| Symptom | Resolution |
|---|---|
| CI lane fails on the branch and on `main` | Pre-existing — file/annotate an issue, don't grind the branch. Merge only if branch-attributable lanes are green. |
| CI flake suspected | One re-run, max. Still red without a `main` repro → treat as branch-caused. |
| Merge conflict | Rebase the feature branch on `main`, re-run targeted tests, push. |
| Docker stack broken after a merge | `make docker-down` + `make docker-build-core` + `make docker-up`; check `make docker-status`. Do not start the next issue on a broken stack. |
| Integration tests skip-green for missing keys | Check which keys the lane needs (AGENTS.md lists them); a skipped test is not a passed test when the skipped behaviour is what the issue shipped. |
| Issue already has an open PR | Yours (this session / a prior run): adopt and drive it to merge. Someone else's: escalate. |
| Issue premise contradicted by evidence | Update or close it with the evidence, comment on the umbrella, continue. Escalate only on material scope change. |
| Context compacted mid-issue | Rebuild from umbrella comments + `docs/plans/` + `git log` + `gh pr list`. |

## Anti-patterns

- **Don't implement issues inline in main.** Even a "one-liner" issue goes through the pipeline — inline fixes skip the critic, the reviewer, and the behaviour-locking test.
- **Don't batch unrelated issues into one PR.** 1 issue = 1 PR. Bundle only true duplicates, recorded in both issues.
- **Don't merge on red**, or on "probably flaky" without a `main` comparison.
- **Don't skip post-merge validation because CI is green.** CI lanes don't cover everything (integration lanes auto-skip without keys).
- **Don't leave bookkeeping for the end.** Umbrella comments and issue state are updated per-issue, not in a final sweep — compaction can hit at any time.
- **Don't re-ask for permissions this skill grants**, and don't assume ones it excludes.
- **Don't parallelize issues.** Shared working tree; serial merges keep every rebase trivial.
