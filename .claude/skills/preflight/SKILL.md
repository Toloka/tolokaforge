---
name: preflight
description: >-
  Run the tolokaforge pre-PR checklist in one shot: diff-scoped ruff+black
  check, pytest-marker audit on new test files, forbidden doc-phrasings
  grep, SecretManager-rule secrets-grep, and with_env.sh reminder for
  env-dependent tests. Reports PASS / WARN / FAIL per check with concrete
  file:line pointers. Triggers on: "preflight", "/preflight", "pre-PR
  check", "ready to open a PR", "final check before PR".
---

# Preflight (tolokaforge)

Run a single consolidated pre-PR check against the current branch. Replaces
the four scattered steps in [`CONTRIBUTING.md`](../../../CONTRIBUTING.md)
plus the diff-hygiene rules in
[`AGENTS.md`](../../../AGENTS.md) that PR reviewers otherwise flag one at
a time.

Preflight is **not** a substitute for `/code-review` — it catches mechanical
CI-blockers before you push, not architectural / correctness concerns.

## Step 1 — Compute the diff scope

Everything below operates on this file list, never on the whole repo:

```bash
git diff --name-only main...HEAD          # committed changes
git diff --name-only                      # unstaged
git diff --name-only --cached             # staged
git ls-files --others --exclude-standard  # untracked
```

Union those. Empty scope → exit early with `PASS (no changes)`.

## Step 2 — Formatter drift (diff-scoped)

**Why diff-scoped:** [`AGENTS.md`](../../../AGENTS.md) Known Gotchas #3
and #4 document ~8 files with pre-existing drift. A whole-repo
`ruff format --check` / `black --check` will report those and is not the
user's problem. Only files *in the diff* count.

```bash
uv run ruff format --check <diff .py paths>
uv run black --check          <diff .py paths>
uv run ruff check             <diff .py paths>
```

- If a failure lands on a file the user did not touch (defensive check —
  shouldn't happen given the diff filter, but be explicit): note it and
  mark the check `WARN`, not `FAIL`.
- All-clean → `PASS`.

## Step 3 — Pytest marker audit (new/modified test files)

`pyproject.toml` sets `--strict-markers`; a test with no registered marker
is a CI hard-fail. Registered markers (13 total, `pyproject.toml:234`):
`unit`, `integration`, `slow`, `requires_api`, `requires_docker`,
`requires_browser`, `docker`, `requires_postgres`, `grading`, `security`,
`performance`, `llm`, `canonical`.

For each `tests/**/test_*.py` in the diff:

```bash
uv run pytest --collect-only -q <path>
```

Then for every collected test node, confirm it (or its class / module)
carries at least one registered marker. Missing marker → `FAIL` with the
node id and a suggested marker based on the file path
(`tests/unit/**` → `unit`, `tests/integration/**` → `integration`, etc.).

## Step 4 — Forbidden doc phrasings

Source: the "delete on sight" list in
[`.claude/skills/code-review/SKILL.md`](../code-review/SKILL.md) § docs-
freshness (line 203). For every `.md` / `.rst` in diff (except
`CHANGELOG.md`, which is a journal by design):

```bash
grep -nEi 'previously\b|used to (be|do|X)|before the (refactor|migration|v[0-9])|until v?[0-9]+\.[0-9]+, this|the old (approach|way) (was|were)' <path>
```

Any hit → `FAIL` with file:line and the offending phrase quoted.

## Step 5 — Secrets grep (SecretManager rule)

`AGENTS.md` § Secrets forbids raw `os.environ.get` / `os.getenv` /
`load_dotenv` for credentials outside `tolokaforge/secrets/**`. On new
or modified `.py` in diff (excluding `tolokaforge/secrets/**` and the
static-grep test itself):

```bash
grep -nE 'os\.environ\.(get|getenv)|os\.getenv\(|from dotenv import|load_dotenv\(' <path>
```

Any hit → `FAIL`. Suggested fix in output:
`from tolokaforge.secrets import get_default; get_default().get_secret("KEY_NAME")`.

## Step 6 — `with_env.sh` reminder

If any test file in the diff (new or modified) adds a `requires_api`,
`requires_docker`, or `requires_postgres` marker, print a `WARN` with:

> Reminder: env-dependent tests must be invoked via
> `scripts/with_env.sh uv run pytest …` locally — plain `uv run pytest`
> will silently skip them.

Not a fail — just a nudge.

## Output shape

```
Preflight — <N> files in scope
─────────────────────────────
 Check 1  Formatter drift   PASS
 Check 2  Test markers      PASS
 Check 3  Doc phrasings     FAIL  docs/CONFIG.md:88  "previously X, now Y"
 Check 4  Secrets grep      FAIL  tolokaforge/foo.py:42  os.environ.get("OPENAI_API_KEY")
 Check 5  env-tests warn    WARN  tests/integration/… uses requires_api

Rollup: 2 FAIL, 1 WARN
```

Exit-shaped rollup: any `FAIL` → block; only `WARN` / `PASS` → good to
`git push`.

## Notes

- This skill is diff-only by design. `pre-commit run --all-files` from
  `CONTRIBUTING.md` remains the whole-repo baseline; preflight is a
  faster loop that catches the recurring diff-scoped mistakes.
- Not a hook. Invoked explicitly via `/preflight` when the user is ready
  to push. If it becomes trusted, a `Stop` hook wrapping the same
  checks is a natural follow-up.
- Reads no state Claude Code can't already read; makes no network calls;
  writes nothing.
