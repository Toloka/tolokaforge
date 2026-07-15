---
name: code-review
description: >-
  Review a tolokaforge branch or PR against AGENTS.md rules — surface-failures,
  fail-fast, mocks-hide-problems, SecretManager-only secrets, harness/task-pack
  boundary, preset-registry discipline, type-system fit, root cleanliness,
  script/tool location, doc freshness. Reports concrete violations with
  file:line and a suggested fix, or states explicitly that the diff is clean.
  Triggers on: "review my branch", "code review", "review against AGENTS",
  "hygiene review", "AGENTS review", "review this PR".
---

# Code Review (tolokaforge)

Run a focused review of changes against the rules in this repo's
[`AGENTS.md`](../../../AGENTS.md) and the subsystem docs under `docs/`.
**Mechanical hygiene + architectural fit, not stylistic taste.** The
`claude-review.yml` GitHub workflow already runs a PR hygiene pass on every
PR — this review goes deeper (contracts, test tiers, blast radius), it does
not repeat that pass.

## Default: delegate to the reviewer agent

When invoked as `/code-review`, **delegate** to the `branch-code-reviewer`
agent via the Agent tool. The agent reads this file as its rules.

1. Determine the scope (Step 1 below).
2. Launch the agent:

   ```
   Agent({
     subagent_type: "branch-code-reviewer",
     description: "Branch code review",
     prompt: "Review <scope>. Load the repo's .agents/skills/
       code-review/SKILL.md and AGENTS.md as rule sources. Report
       findings in the structured format defined in the agent spec."
   })
   ```

3. Relay the agent's report to the user verbatim.
4. Offer to auto-apply suggested fixes (Step 5).

**Bypass the agent and run inline** only when the user asks (`/code-review
--inline`) or when an agent harness isn't available. The rule sections
below are the same content the agent loads — use them as the inline
checklist if needed.

## Step 1: Determine review scope

Ask the user only if it's ambiguous. Default behaviour:

| User said | Scope |
|---|---|
| `/code-review` (no args) | Current branch vs `main` (`git diff main...HEAD` + uncommitted) |
| `/code-review <PR#>` | The PR diff (use `gh pr diff <PR#>`) |
| `/code-review <ref>..<ref>` | That git range |
| Names a file or directory | Just that path |

Always reconcile with the working tree: `git status` + `git diff` so you
also catch uncommitted local edits.

## Step 2: Gather the diff and the relevant rule context

1. Get the diff:
   ```bash
   git diff <base>...HEAD          # branch review
   git diff                        # uncommitted
   gh pr diff <PR#>                # PR review
   ```
2. List touched files and which subsystem(s) they belong to
   (`tolokaforge/cli`, `tolokaforge/core`, `tolokaforge/core/llm`,
   `tolokaforge/runner`, `tolokaforge/adapters`, `tolokaforge/secrets`,
   `tolokaforge/tools`, `tolokaforge/env`, `tools/`, `scripts/`,
   `tests/`, `docs/`, task packs, …).
3. Read the root [`AGENTS.md`](../../../AGENTS.md) and the relevant
   subsystem doc(s): `docs/LLM_LAYER.md` for `core/llm`, `docs/TASKS.md`
   / `docs/GRADING.md` for tasks and grading, `docs/RUNNER.md` for
   runner/Docker, `docs/ADAPTERS.md` for adapters, `tests/README.md`
   for test changes, `scripts/README.md` for scripts.
4. Read full files (not just hunks) when a finding hinges on
   surrounding context.

## Step 3: Run the checks

Walk through every category. For each violation, record: **file:line**,
**rule it breaks**, **why it matters**, **concrete fix**. If nothing in
a category violates, skip it silently.

### 1. CODE QUALITY — surface failures explicitly

- No silent fallbacks. No `except Exception: pass`. No
  `except Exception: return None` where the caller would have wanted an
  error. No swallowed `subprocess.run` failures (`check=False` without
  an explicit decision). No `try/except` that hides a bug rather than
  handles a known condition.
- No `if x is None: return None` chains that mask a missing prereq
  silently — raise.
- No "log and continue" on operations that should fail loudly.
- Function length: split functions over **100 lines**. Flag god classes.
- Nesting depth: refactor anything reaching **≥ 3** levels — prefer
  early return on the inverse condition, extracted helper, or early
  `continue` / `break`.
- No suppressed lint warnings (`# noqa`, `# type: ignore`) without a
  one-line justification. Don't suppress warnings — update code to use
  the actual functionality.

### 2. ARCHITECTURE BOUNDARIES

- **Secrets go through `SecretManager` — only.** No `os.environ.get` /
  `os.getenv` for credentials, no `load_dotenv()` outside
  `DotEnvProvider`, no direct `.env` / `.netrc` / credentials-file
  reads, no secrets in Docker images/build args/tags.
  `export_to_environ(keys)` only in the smallest scope when a
  subprocess demands it. The static-grep test
  (`tests/unit/secrets/test_no_raw_secret_access.py`) enforces most of
  this; flag any new pattern it wouldn't catch.
- **Harness logic is generic.** Task-specific logic (domain field
  names, per-task prompts or branches) belongs in task packs, never in
  `tolokaforge/` engine code (Core Rule 2).
- **No model-name conditionals.** All model-specific behaviour goes
  through the preset registry and `ModelCapabilities` policy slots
  (`SystemPromptPolicy`, `ToolSchemaSanitizer`, `ResponsePolicy`,
  `ReasoningCodec`, …) — never `if "gemini" in model:` in engine code.
- **Type-system fit** (AGENTS.md table): behaviour contracts are
  `Protocol`/`ABC`; in-process value objects are frozen `dataclass`;
  named values are `str, Enum`; anything crossing a serialisation
  boundary (gRPC, JSON, YAML, output bundle, snapshot) is Pydantic v2
  with `extra="forbid"`. Extending an existing contract follows its
  existing choice; changing the choice needs a stated reason (its own
  PR).
- **`contrib/` is protected.** Any diff touching it goes through the
  vendoring process — flag it.
- **Compatibility surfaces vs internals.** Task contracts, task-pack
  formats, run-config schemas, the CLI, and the published Python API
  change only with an explicit migration — CHANGELOG entry + docs
  (Core Rule 5). Internal code is the opposite: no `_legacy_*`
  aliases, no duplicate exports, no deprecation wrappers, no "rename
  later" — internals refactor cleanly in one commit.

### 3. TESTING

- **Test behaviour, not code.** Flag tests that only assert mocked
  return values, tests of pydantic models, tests of language features,
  or tests that recreate the implementation.
- **Right tier.** `unit` (pure logic, no services), `canonical`
  (contract/snapshot — schema shapes, policy routing), `integration`
  (real services/keys, run via `scripts/with_env.sh`). New behaviour
  gets a test at the tier where the behaviour is real. Deep mocking
  that recreates the system under test is a smell.
- **Canonical snapshots are regenerated, never hand-edited** — dev MCP
  `update_canonical_snapshots` (`--update-canon`), with the diff
  reviewed deliberately.
- **Don't accumulate.** If a test only exercises mocking infrastructure
  (asserts on `mock.call_count`, `mock.assert_called_with(...)` with no
  behavioural meaning), it should be deleted, not added.
- **Extend, don't duplicate.** For every NEW test in the diff, check
  what already covers its target (`rg` the tested class / function /
  preset across `tests/`): a new test that re-locks behaviour an
  existing test already locks is a Major — name the existing test;
  extending it is the fix. Copy-paste variants differing in one value
  are one parametrized test. This check is the sanctioned exception to
  the no-scope-creep rule.
- New tests must carry the right marker so CI's test matrix picks them
  up; an integration test without its marker silently escapes CI.

### 4. REPOSITORY HYGIENE

- **Root cleanliness.** New files in the repo root must be on the
  allow-list — README/LICENSE/CHANGELOG/CONTRIBUTING/CONTRIBUTORS/
  CITATION/AGENTS.md/CLAUDE.md/pyproject.toml/uv.lock/Makefile and
  dotfiles. No data files, logs, scratch docs, or one-off scripts in
  the root.
- **No temporary artifacts committed.** `plans/` is gitignored (local
  planning only); permanent development plans live in `docs/`. Never
  commit log files, JSON dumps, build outputs, scratch documents. Data
  files belong in `tests/data/`, `contrib/`, or task fixture
  directories.
- **Scripts location.** Bash scripts live in `scripts/<category>/`
  (`benchmark/`, `setup/`, `lint/`, `tests/`, `release/`,
  `analysis/`); shared utilities (`common.sh`, `with_env.sh`) at
  `scripts/` root. Complex Python logic goes to `tools/` as a uv
  workspace member (registered in `[tool.uv.workspace]`, runnable via
  `uv run <tool-name>`), with a simple bash wrapper in `scripts/` for
  common usage. See `scripts/README.md`.
- **uv workspace rules.** No `[project.optional-dependencies]` in
  workspace members; dev deps in root `[dependency-groups]` → `dev`;
  members reference each other with `{ workspace = true }`.
- **No project-specific content on `main`.** No domain-specific
  configs or proprietary runner scripts. A run config lives next to
  the example it runs (`examples/<adapter>/<family>/run_config.yaml`)
  — there is no separate `config/` directory.

### 5. DOCUMENTATION — ALWAYS CURRENT, ONLY ACTUAL STATE

**Binding rule (Blocker, not nit — AGENTS.md Core Rule 8):**
source-of-truth docs (`AGENTS.md`, `docs/*.md`, `README.md`,
`tests/README.md`, `scripts/README.md`) describe the system *as it is
now*. There is no past tense, no "previously", no history.

- Any change in user-facing behaviour, contract, or developer command
  requires a docs update **in the same PR**.
- Rewrite affected sections so they read as if the new state is the only
  state. **Forbidden phrasings (delete on sight):**
  - "Previously X, now Y" / "this used to be X"
  - "Before the refactor / migration / vN.N"
  - "Until <date or version>, this did X"
  - "The old approach was X" / "we used to X"
  - Migration history blocks ("v1 did X, v2 did Y, v3 does Z")
  - Removed behaviour described in past tense
- Renamed / moved / deleted thing? `rg <old-name>` across the repo —
  every stale mention is fixed or deleted in this PR.
- Migration / decision history belongs in git log, `CHANGELOG.md`, and
  the PR description, not in docs.
- New developer-facing command? Add it to root `AGENTS.md` "Setup and
  Commands" (and the Makefile `help` target if it's a make target).
- **Skills and agent specs are docs too.** `.agents/skills/*/SKILL.md`
  and `.claude/agents/*.md` describe workflows, commands, and tools as
  they are *now*. A diff that renames a make target, changes a dev-MCP
  tool, moves a doc, or reshapes a workflow those files reference must
  update them in the same PR — same rule, same severity.

**Exception:** `CHANGELOG.md` is a journal — it records decisions as
they were made and may be historical by nature. It is *not* a source
of truth about current behaviour. Anything describing how the system
works today belongs in the relevant `docs/*.md` / `AGENTS.md`.

### 6. SCRIPT / SHELL STANDARDS

- **Every bash script** uses `set -euo pipefail`. Bare `set -e` is a
  smell — flag and upgrade.
- Scripts that need `.env` variables go through `scripts/with_env.sh`
  rather than re-implementing dotenv loading (which would also trip
  the secrets rule).
- New scripts follow `scripts/README.md` placement and get a Makefile
  target when they're part of the everyday loop.

### 7. PYTHON STYLE

- DRY. Pull shared logic into a helper module.
- Self-describing names. No comments restating *what* — only *why*.
- Use `uv run`, never raw `python` / `pip install` / `.venv/bin/python`.
- Preferred libraries per AGENTS.md: `typer` for CLI parsing,
  `tenacity` for retry logic.

### 7a. COMMENT & DOCSTRING HYGIENE

Default to **no comment**. Add one only when removing it would leave a
future reader genuinely confused. The hidden cost of comments is that
they rot — code moves, names change, issues close, but the comment
stays and starts to lie.

**Flag and DELETE these patterns:**

- **Tautologies that restate the signature.**
  ```python
  # Bad — the absence of a default already says required
  error_kind: FailureKind  # REQUIRED — no default
  attempt: int  # REQUIRED — see class docstring
  ```
- **Issue / stage / PR attribution.** Use `git blame`, not a comment.
  ```python
  # Bad
  # Added in #237 stage 1. Wire contract per AGENTS.md Core Rule 4.
  # Issue #237 stage 2: classifier inlined verbatim.
  ```
- **AGENTS.md / rulebook citations at the callsite.** Quote rules in
  `AGENTS.md`, not at every callsite that obeys them.
  ```python
  # Bad
  # AGENTS.md Core Rule 5 (compatibility surfaces): no default on
  # this field.
  ```
- **Future-tense planning.** Either the future has happened (describe
  current behavior) or it hasn't (TODO at most, with an issue link).
  ```python
  # Bad
  # Hardcoded to 1 here; stage 3 will derive from TrialStatus.
  ```
- **Restating the function name.**
  ```python
  # Bad — function is already named _extract_attempt
  def _extract_attempt(kwargs: dict) -> int:
      """Pull the 1-indexed attempt from job kwargs."""
  ```
- **Module migration history in docstrings.**
  ```python
  # Bad — module docstring
  """Stage 1 landed the types. Stage 2 implements classify_failure.
  Stage 3 will integrate with the orchestrator."""
  ```

**KEEP these patterns — they describe non-obvious behavior:**

- **Hidden constraint that surprises a refactorer.**
  ```python
  # Good — without this, a refactorer would naturally deduplicate
  # This codec is reconstructed inside the runner container from
  # TOLOKAFORGE_SECRETS_JSON; it must stay stdlib-only.
  ```
- **Load-bearing invariant the type system can't express.**
  ```python
  # Good — invariant the reader needs to preserve
  # encode_for_replay drops blobs < 100 chars: OpenRouter sends a
  # constant placeholder when Gemini emitted no real thinking, and
  # replaying it creates few-shot patterns the model echoes back.
  ```
- **Workaround that looks wrong but isn't.**
  ```python
  # Good — without this, a reader would file a bug
  # Recovery is scoped to declared-array tags sites only; a
  # schema-agnostic rewrite corrupts scalar fields on M2.7.
  ```
- **Surprising default.**
  ```python
  # Good — explains why the fallback exists at all
  # Defaults to the core profile: the full stack needs rag-service +
  # mock-web images most contributors haven't built.
  ```

When in doubt: **delete it**. If the next reader needs the context,
they'll find it in `git log`, the PR description, or AGENTS.md.

### 8. DOCKERFILE GUIDELINES (when touching images)

- Multi-stage builds; build deps separate from runtime.
- Layer order: less-frequently-changing instructions first; copy
  `pyproject.toml`/`uv.lock` before source for caching; combine `RUN`
  instructions with `&&`.
- Non-root runtime user named `runner`; minimal pinned base image
  (never `latest`); `COPY` not `ADD`.
- `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`. Use BuildKit cache
  mounts for the uv/pip cache.
- `.dockerignore` keeps the build context lean.
- Consistent casing: `FROM base AS builder`, not `as builder`.

### 9. TASK DESIGN QUALITY (when touching task packs)

- No tasks that always pass; target useful difficulty.
- No walkthrough-style scripted prompts — natural user requests.
- Grading checks agent-produced outcomes, not default/pre-filled
  values.
- App/task state routes through the state service so grading can
  verify deterministically.
- Tool parameters are strict Pydantic models with explicit fields —
  `dict[str, Any]` parameters defeat schema enforcement (AGENTS.md
  gotcha #24) and the fix belongs in the task pack.

### 10. MCP USAGE (when reviewing diffs that touch agent flows)

- Library/framework changes that look guessed-at: flag as "consider
  Context7 lookup".
- Agent-facing docs or skills that instruct raw `pytest` / `ruff`
  invocations where the dev MCP tools (`run_tests`, `lint_check`,
  `format_check`) exist: flag for consistency.

## Step 4: Report

Output structure:

```
## Review Summary

<one-paragraph verdict: clean / N findings>

## Findings

### 1. <category>: <one-line headline>

- File: `path/to/file.py:42`
- Rule: <name of rule from AGENTS.md or a section above>
- Why it matters: <one sentence>
- Suggested fix:
  ```diff
  - <bad line>
  + <good line>
  ```
  Or: `<concrete prose instruction if a diff doesn't fit>`

### 2. ...

## Clean Categories

- <list categories with zero findings, terse>

## Open Questions

- <list anything that needs the user's judgement>
```

If everything checks out, the report is one line: **"Reviewed
<scope>. No AGENTS.md violations."** Be willing to say this. Don't
invent findings.

## Step 5: Offer to fix (optional)

After delivering the report, ask the user whether you should
auto-apply any of the "Suggested fix" diffs. Apply only what they
greenlight; never proactively edit during the review pass.

## Anti-patterns to avoid in your own review

- **Don't invent findings.** "Could be cleaner" is not a violation.
  Either it breaks a rule above or it doesn't.
- **Don't lecture on style.** This skill enforces the listed rules.
  Personal taste belongs in a code-review conversation, not a
  hygiene report.
- **Don't review code you didn't read fully.** If a finding hinges on
  caller behaviour, read the caller. No reading = no finding.
- **Don't repeat what ruff / pre-commit / the secrets static-grep /
  the canonical certificate test already flag.** Assume CI catches
  mechanical issues; concentrate on things humans miss.
- **Don't gate on "nice-to-have" suggestions.** Distinguish
  *violation* (must-fix) from *suggestion* (consider) — label clearly.
