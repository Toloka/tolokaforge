# Releasing Tolokaforge

A tolokaforge release has three independent axes, each cut by its own tag push
and each with its own GitHub Actions workflow:

- **The `tolokaforge` PyPI package** — `vX.Y.Z` tags, cut automatically by the
  "Release (cz bump)" workflow.
- **The `tolokaforge-models` PyPI package** — `models-vX.Y.Z` tags, cut
  automatically by the "Release tolokaforge-models (cz bump)" workflow.
- **The Docker images** — the `image-vX.Y.Z-rc.1` release-candidate tag is cut
  automatically by the "Release (cz bump)" workflow; the stable
  `image-vX.Y.Z` tag is pushed by hand once the rc is verified.

The engine and Docker-image axes share a single version number: the PyPI
release sets it (it owns `[project].version` in the workspace-root
`pyproject.toml`), and the image workflow refuses to publish unless the image
tag agrees with it. The `tolokaforge-models` axis versions independently —
its `[project].version` lives in `tolokaforge_models/pyproject.toml`, and its
tag namespace (`models-v*`) does not overlap the engine's (`v*`) or the
images' (`image-v*`).

## PyPI package — `vX.Y.Z` (automated)

The package release is driven entirely by
[`.github/workflows/release.yml`](../.github/workflows/release.yml) ("Release (cz
bump)"). Run it from the Actions tab as a manual `workflow_dispatch`:

- **`bump`** — `auto` (derive the increment from the Conventional Commits since
  the last tag: `fix` → patch, `feat` → minor, `feat!` / `BREAKING CHANGE` →
  major), or force `patch` / `minor` / `major`.
- **`dry_run`** — compute the bump and changelog and print them without
  committing, tagging, or pushing. A safe preview.

A real run executes `cz bump`, which:

1. Bumps `[project].version` in `pyproject.toml`.
2. Regenerates `CHANGELOG.md` from the Conventional Commit history.
3. Relocks `uv.lock` to the new version (a commitizen pre-bump hook) and folds it
   into the release commit.
4. Commits `chore(release): bump version to X.Y.Z`.
5. Creates and pushes the annotated `vX.Y.Z` tag.

The version and changelog are owned by commitizen. **Do not hand-edit
`[project].version` or `CHANGELOG.md`** — the bump derives both, and manual edits
desync the two.

Pushing the `vX.Y.Z` tag then triggers two tag-driven workflows automatically:

- [`publish-tolokaforge.yml`](../.github/workflows/publish-tolokaforge.yml) —
  builds the wheel + sdist and publishes them to PyPI via OIDC trusted
  publishing (no stored token), then creates a GitHub Release.
- [`release-gate.yml`](../.github/workflows/release-gate.yml) — runs lint, task
  validation, and the unit / canonical / integration test lanes against the
  tagged commit.

If `auto` finds no releasable commits since the last tag, the workflow exits
cleanly without cutting a release.

## PyPI package — tolokaforge-models `models-vX.Y.Z` (automated)

The `tolokaforge-models` wheel — model data files, the 39 `ModelCertificate`
registry, and the eight per-model policy subclasses — ships on its own release
cadence. The release is driven entirely by
[`.github/workflows/release-models.yml`](../.github/workflows/release-models.yml)
("Release tolokaforge-models (cz bump)"). Run it from the Actions tab as a
manual `workflow_dispatch`:

- **`bump`** — `auto` (derive the increment from the Conventional Commits
  since the last `models-v*` tag: `fix` → patch, `feat` → minor,
  `feat!` / `BREAKING CHANGE` → major), or force `patch` / `minor` / `major`.
- **`dry_run`** — compute the bump and changelog and print them without
  committing, tagging, or pushing.

A real run enters the `tolokaforge_models/` directory and executes
`cz bump` against the per-project commitizen config
(`tolokaforge_models/pyproject.toml` `[tool.commitizen]`). The bump:

1. Rewrites `tolokaforge_models/pyproject.toml` `[project].version` and
   `tolokaforge_models/src/tolokaforge_models/__init__.py` `__version__`.
2. Regenerates `tolokaforge_models/CHANGELOG.md` from the Conventional
   Commit history.
3. Relocks the workspace-root `uv.lock` (a commitizen pre-bump hook) and
   folds it into the release commit.
4. Commits `chore(models-release): bump tolokaforge-models to X.Y.Z`.
5. Creates and pushes the annotated `models-vX.Y.Z` tag.

Pushing the `models-vX.Y.Z` tag triggers
[`publish-tolokaforge-models.yml`](../.github/workflows/publish-tolokaforge-models.yml),
which builds the wheel with `uv build --package tolokaforge-models` and
publishes it to PyPI via OIDC trusted publishing (no stored token), then
creates a GitHub Release. The engine's `Release Gate` workflow does not run
against a `models-v*` tag — engine and models test lanes are separately
sequenced.

The version and changelog are owned by commitizen. **Do not hand-edit
`tolokaforge_models/pyproject.toml` `[project].version` or
`tolokaforge_models/CHANGELOG.md`** — the bump derives both, and manual edits
desync the two.

### Trusted publisher configuration

The PyPI project `tolokaforge-models` is registered with a Trusted Publisher
configuration bound to this repository:

| Field | Value |
| --- | --- |
| Owner | `Toloka` |
| Repository | `tolokaforge` |
| Workflow filename | `publish-tolokaforge-models.yml` |
| Environment | `release` |

The publish job on `publish-tolokaforge-models.yml` declares
`environment: release` and `permissions: id-token: write`; PyPI's Trusted
Publisher backend resolves the workflow's OIDC token against the entry above
at publish time. No API token is stored in the repository. The TestPyPI
counterpart follows the same shape under the `testpypi` environment.

### TestPyPI dry run

To validate the build + publish path without cutting a real release, run
`publish-tolokaforge-models.yml` from the Actions tab as a
`workflow_dispatch` with `target = testpypi`. The workflow builds the wheel,
uploads it as an artifact, and publishes to
[test.pypi.org](https://test.pypi.org/project/tolokaforge-models/) under the
`testpypi` environment via `uv publish ... --publish-url
https://test.pypi.org/legacy/ --trusted-publishing always`. No tag is
required and no PyPI release is cut. Bump `[project].version` locally on a
throwaway commit if the run overlaps an already-published version — TestPyPI
refuses re-uploads of a used version string.

## Docker images — `image-vX.Y.Z-rc.1` (auto) and `image-vX.Y.Z` (manual)

The four first-party images —
`tolokasoft1/tolokaforge-{runner,db-service,rag-service,mock-web}` — are published
by [`publish-images.yml`](../.github/workflows/publish-images.yml), which fires on
any pushed `image-v*` tag. The release workflow cuts the release-candidate tag
`image-vX.Y.Z-rc.1` for you; the stable tag `image-vX.Y.Z` is pushed by hand once
the rc is verified. The workflow builds the wheel once; only the images that ship
it — `runner` and `rag-service` — are layered from that artifact, while
`db-service` and `mock-web` build without it.

Images are `linux/amd64` only. The standalone compose recipe pins
`platform: ${TOLOKAFORGE_PLATFORM:-linux/amd64}`, so Apple-Silicon hosts run the
published images under emulation.

### Version guard

Before building, the workflow asserts that `image-v<base>` equals the
`pyproject` `[project].version`, with any `-rc.N` suffix stripped from the tag
first. The image tag must therefore match the package version cut by the PyPI
release. The two axes share one version number but are separate tag pushes.

### Release-candidate, then stable

Every image release goes through a release candidate first.

1. **The rc tag is cut for you.** When "Release (cz bump)" cuts `vX.Y.Z`, it also
   pushes `image-vX.Y.Z-rc.1`. That routes through the `pre-stable` deployment
   environment, pushes the immutable `:X.Y.Z-rc.1` tags, and runs a keyless
   rc-smoke against the freshly-pushed images. It does **not** move `:latest` or
   `:X.Y`.

2. **Verify the rc** — pull the `:X.Y.Z-rc.1` images and smoke them yourself.

3. **Push the stable tag.**

   ```bash
   git tag image-vX.Y.Z
   git push origin image-vX.Y.Z
   ```

   This routes through the `release` environment and pushes the immutable
   `:X.Y.Z` tags. Only **after** the smoke gate passes does the workflow move the
   `:latest` and `:X.Y` moving tags onto that digest, then publish a GitHub
   Release listing each image's digest.

### Dry run

Running `publish-images.yml` from the Actions tab as a `workflow_dispatch` builds
all four images **without** logging in or pushing — a safe pre-check that the
Dockerfiles and wheel still build.

## Typical order

The engine + image release and the `tolokaforge-models` release are
independent axes, each with its own tag and workflow. But they are **not
symmetric on first publish** — the engine wheel declares
`tolokaforge-models>=1.0.0,<2.0.0` in `[project].dependencies`, so
`pip install tolokaforge==<version>` fails to resolve until the models
wheel exists on PyPI. **Publish `tolokaforge-models` first, then the
engine.** After both have shipped once, subsequent releases on either
axis are order-independent.

For the first-time cutover (this milestone) — models before engine:

1. Run "Release tolokaforge-models (cz bump)" to cut `models-v1.0.0` and
   publish `tolokaforge-models 1.0.0` to PyPI. Verify the wheel appears
   at https://pypi.org/project/tolokaforge-models/.
2. Then run "Release (cz bump)" to cut `vX.Y.Z` on the engine, publish
   `tolokaforge`, and push `image-vX.Y.Z-rc.1`.
3. Verify the rc images.
4. `git tag image-vX.Y.Z && git push origin image-vX.Y.Z` to publish
   stable images.

For any subsequent release once both wheels have shipped, either axis can
go first:

- **Engine + image axis** — same steps 2-4 above; `pip install tolokaforge`
  resolves `tolokaforge-models` from PyPI (existing 1.x satisfies the pin).
- **`tolokaforge-models` axis** — "Release tolokaforge-models (cz bump)"
  cuts `models-vX.Y.Z`, regenerates `tolokaforge_models/CHANGELOG.md`,
  publishes to PyPI. No image tag; the wheel ships data, not runtime.

### `models-v1.0.0` is hand-tagged

`release-models.yml`'s `cz bump` derives an *increment* from the
Conventional Commits since the last `models-v*` tag. Before the first
publish there is no such tag, so a bump would land the wheel *past* 1.0.0
even with `models_v1.0.0`'s pyproject already at `1.0.0`. **The very
first models release is created by hand**:

```bash
git tag models-v1.0.0
git push origin models-v1.0.0
```

The tag push fires `publish-tolokaforge-models.yml`, which builds the
wheel and uploads via OIDC. All *subsequent* models releases go through
`release-models.yml`.

### commitizen scope limitation

`release-models.yml` runs `cz bump` with `working-directory:
tolokaforge_models`. That controls which config commitizen reads and
where it writes; it does NOT filter the *commits* commitizen scans.
Every conventional commit reachable from `HEAD` since the last
`models-v*` tag participates in the increment derivation. In practice
this rarely matters — Milestone 29's split is why per-tree cadence
exists — but if an engine-only PR lands a `feat!:` between two models
releases, the next `models-v*` bump reads it as `major`. Override with
`--increment=patch` / `minor` / `major` on `workflow_dispatch` when
that mismatch shows up.

## Downstream data-resource consumers

The `tolokaforge-models` wheel ships three bundled data files external
tooling reads at runtime: `pricing.json`, `model_presets.yaml`,
`providers.yaml`. Downstream consumers (leaderboards, cost analysers,
integration scripts) must reach these through the public accessor API
on `tolokaforge.core.model_data` rather than through raw
`importlib.resources` lookups:

```python
from tolokaforge.core.model_data import (
    bundled_pricing_path,
    bundled_presets_path,
    bundled_providers_path,
)

pricing_path = bundled_pricing_path()  # -> pathlib.Path
```

Each accessor returns a `pathlib.Path` when the file exists and raises
`FileNotFoundError` when it does not. Both surfaces are public API, stable
within the `v0.17.x` minor series; signature or semantic changes require a
deprecation announcement in the CHANGELOG.

### Missing vs empty — the fail-loud split

The accessor and the consumer share a two-layer fail-loud contract:

- **Accessor layer** — verifies the resource file exists on disk. Raises
  `FileNotFoundError` if it does not. Does NOT read or validate contents.
- **Consumer layer** — verifies the file's parsed content is well-formed
  and non-empty. Raises `ValueError` on malformed JSON/YAML, on an empty
  document, or on a top-level payload of the wrong shape.

Downstream consumers must NOT skip the empty-content check on their side.
An empty-but-present file is not a supported install shape; treating it as
"pricing is `{}`" produces silent zero-cost leaderboards, which is worse
than a loud startup error.

Never wrap either layer in a try/except that swallows the exception and
returns an empty dict. Silent-swallowing a missing pricing table produces
zero-cost columns in leaderboards — a silent-wrong outcome AGENTS.md Core
Rule 1 rejects. Downstream consumers must let the raise propagate to
startup, not translate it into an empty mapping.

### Resource location

The accessors abstract the resource location. The module-level
`_DATA_ROOT` constant in
[`tolokaforge/core/model_data.py`](../tolokaforge/core/model_data.py)
points at `tolokaforge_models.data`; consumer code depends only on the
`Path` returned and the `FileNotFoundError` semantics.

See [ADR-0030 § "Downstream data-resource consumers"](adr/0030-tolokaforge-models-split.md#downstream-data-resource-consumers-new--widening-revised-2026-08-07)
for the rationale ADR-0030 chose fixing consumers up-front over shipping a
forwarding stub in the engine.

### Bumping `minimum_engine_version` on the models wheel

The `tolokaforge_models.minimum_engine_version` PEP 440 specifier
(declared on
[`tolokaforge_models/__init__.py`](../tolokaforge_models/src/tolokaforge_models/__init__.py))
is a hard install-time constraint. The engine reads it at
[`tolokaforge.core.llm.presets`](../tolokaforge/core/llm/presets.py)
import via
[`_check_minimum_engine_version()`](../tolokaforge/core/model_data.py)
and refuses to boot when the installed engine version does not satisfy
the specifier — see
[`docs/LLM_LAYER.md` § "Startup validation"](LLM_LAYER.md#startup-validation).

A release that widens the floor (for example
`>=0.17,<0.18` → `>=0.18,<0.19`) forces every consumer to either upgrade
the engine wheel or downgrade the models wheel; users on the older
engine see a startup `RuntimeError` naming both versions. Land the
matching engine minor bump in the same release cycle, and call the
migration out in the CHANGELOG.

## See also

- [Standalone Runner Guide](STANDALONE_RUNNER.md#published-images) — the published
  image tag axis and how consumers pull the images.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — Conventional Commits, the input the
  automated bump reads.
