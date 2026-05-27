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
scripts/tests/public_export_smoke.sh
```

## Pull Request Guidelines

1. Keep changes scoped and atomic.
2. Add/adjust tests with behavior changes.
3. Update docs when user-facing behavior changes.
4. Do not include private/internal benchmark content in this repository.

## Public Export Policy

This repository is intended to be open-source harness code with public examples only.

1. Proprietary tasks and internal integrations must stay out of the public export.
2. Use `scripts/release/prepare_public_export.sh` for release packaging.
3. Use `scripts/tests/public_export_smoke.sh` to verify boundaries and leak policy.
