# PyPI Publishing Setup

This document describes how to configure PyPI publishing for the `tolokaforge` monorepo.

## Overview

The repository publishes two packages to PyPI:

| Package | Tag pattern | Workflow |
|---|---|---|
| `tolokaforge` | `v*` (e.g., `v0.1.0`) | `.github/workflows/publish-tolokaforge.yml` |
| `tolokaforge-adapter-terminal-bench` | `adapter-terminal-bench-v*` (e.g., `adapter-terminal-bench-v0.1.0`) | `.github/workflows/publish-adapter-terminal-bench.yml` |

Both workflows use **OIDC Trusted Publishers** — no API tokens are stored in secrets.

## One-Time Setup

### 1. Create GitHub Environments

In repo **Settings → Environments**, create two environments:

**`release`** (production PyPI):
- Add deployment protection rule: restrict to tags matching `v*`
- This environment is used for publishing to pypi.org

**`testpypi`** (pre-release validation):
- No protection rules needed
- This environment is used for manual TestPyPI publishes

### 2. Register Trusted Publishers on PyPI

#### For `tolokaforge`:

1. Log in to [pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)
2. Under **Add a new pending publisher**, enter:
   - **PyPI project name**: `tolokaforge`
   - **Owner**: `Toloka`
   - **Repository name**: `tolokaforge`
   - **Workflow name**: `publish-tolokaforge.yml`
   - **Environment name**: `release`
3. Click **Add**

#### For `tolokaforge-adapter-terminal-bench`:

1. Same page, add another pending publisher:
   - **PyPI project name**: `tolokaforge-adapter-terminal-bench`
   - **Owner**: `Toloka`
   - **Repository name**: `tolokaforge`
   - **Workflow name**: `publish-adapter-terminal-bench.yml`
   - **Environment name**: `release`
2. Click **Add**

### 3. Register Trusted Publishers on TestPyPI

Repeat the same steps on [test.pypi.org/manage/account/publishing/](https://test.pypi.org/manage/account/publishing/), but use **Environment name**: `testpypi` and the matching workflow names.

## Release Process

### Pre-release validation (TestPyPI)

1. Go to **Actions → Publish tolokaforge to PyPI** (or the adapter workflow)
2. Click **Run workflow**
3. Select target: **testpypi**
4. Verify the package at `https://test.pypi.org/project/tolokaforge/`
5. Test installation: `pip install -i https://test.pypi.org/simple/ tolokaforge`

### Production release

Release the adapter first (if changed), then tolokaforge:

```bash
# 1. Release adapter (if version changed)
git tag adapter-terminal-bench-v0.1.0
git push origin adapter-terminal-bench-v0.1.0

# 2. Release tolokaforge
git tag v0.1.0
git push origin v0.1.0
```

The tag push triggers the workflow automatically:
- Builds sdist + wheel with `uv build`
- Publishes to PyPI via OIDC trusted publisher
- Creates a GitHub Release with auto-generated notes

### Manual PyPI publish (workflow_dispatch)

Both workflows also support manual triggers. Go to **Actions** → select the workflow → **Run workflow** → choose `pypi` or `testpypi`.

## Local Build Verification

```bash
# Build tolokaforge
make build

# Build adapter
make build-adapter

# Build both
make build-all

# Inspect wheel contents
unzip -l dist/*.whl
```

## Version Management

Versions are tracked in two places for `tolokaforge`:
- `pyproject.toml` → `[project] version`
- `tolokaforge/__init__.py` → `__version__`

Both must be updated together before tagging a release.

For the adapter, version is only in:
- `external_adapters/tolokaforge-adapter-terminal-bench/pyproject.toml` → `[project] version`
