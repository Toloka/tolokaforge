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
