# Contributing to Tolokaforge

Thanks for contributing.

## Development Setup

1. Install dependencies:
```bash
make install
```
2. Install dev tooling:
```bash
make install-dev
uv run playwright install --with-deps chromium
```
3. Configure API keys (optional for local lint/unit):
```bash
cp .env.example .env
```

## Local Checks Before PR

Run these before opening a pull request:

```bash
uv run pre-commit run --all-files
uv run pytest tests/unit/ -v
```

## Pull Request Guidelines

1. Keep changes scoped and atomic.
2. Add/adjust tests with behavior changes.
3. Update docs when user-facing behavior changes.
4. Do not include private/internal benchmark content in this repository.
5. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
   and PR titles (`feat(scope): …`, `fix(scope): …`, `chore: …`). The release
   tooling derives the version bump and CHANGELOG from these.

## Issue Lifecycle

Every open issue lands via one of the three Issue Forms in
`.github/ISSUE_TEMPLATE/` — `bug.yml`, `enhancement.yml`, or `chore.yml`.
Each form requires a `priority` (P0–P3) and stamps a matching `type`
label on submit; milestone assignment is a triage decision, not an
intake gate.

### Priority

| Level | Meaning |
| --- | --- |
| P0 | Blocks production, a shipped commitment, or a core user flow. |
| P1 | Important UX / stability / correctness gap; schedule soon. |
| P2 | Improvement or hardening, not user-blocking. |
| P3 | Cleanup, nitpick, deferred improvement. |

### Umbrella epic closure

An umbrella epic tracks a milestone or a themed set of child issues.
Because feature-half work and downstream measurement typically ship on
different cadences, umbrellas need an explicit closure rule:

- **Close when** all child issues are closed **and** one of these is
  true: the feature has landed, the measurement is complete, or the
  remainder has been explicitly deferred to a new umbrella.
- **Keep open** while the feature-half PR has shipped but sub-issues
  remain, **or** while a downstream measurement task is still pending.
- **Split** when the umbrella has been open more than 90 days with the
  feature done but measurement pending — split the measurement into a
  fresh issue, then close the umbrella.

Closing an umbrella by hand: comment the consolidation PR number and
name the closure condition that was met (`all child issues closed`,
`feature landed`, `measurement complete`, or `deferred to <new epic>`).

### Weekly triage

The backlog is triaged weekly by the maintainer. Triage is advisory —
it never auto-closes issues. A quarterly deep pass adds duplicate
clustering and adversarially-verified "already solved" closure
candidates; each candidate carries its evidence and is confirmed by a
human before the issue is closed.

## Cutting a Release

Tolokaforge ships on three independent tag axes: the `tolokaforge` PyPI
package (`vX.Y.Z`), the `tolokaforge-models` PyPI package (`models-vX.Y.Z`),
and the Docker images (`image-vX.Y.Z` after an `image-vX.Y.Z-rc.1` rc). The
engine and image axes share a single version number and are cut together by
the "Release (cz bump)" workflow; the `tolokaforge-models` axis versions
independently and is cut by the "Release tolokaforge-models (cz bump)"
workflow. The full procedure — both cz-bump workflows, the
`image-vX.Y.Z` rc-then-stable flow, the version guard between engine and
image tags, and the PyPI Trusted Publisher configuration — is documented in
[docs/RELEASING.md](docs/RELEASING.md).
