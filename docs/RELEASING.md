# Releasing Tolokaforge

A tolokaforge release has two independent axes, each cut by its own tag push and
each with its own GitHub Actions workflow:

- **The PyPI package** — `vX.Y.Z` tags, cut automatically by the "Release (cz
  bump)" workflow.
- **The Docker images** — the `image-vX.Y.Z-rc.1` release-candidate tag is cut
  automatically by the same "Release (cz bump)" workflow; the stable
  `image-vX.Y.Z` tag is pushed by hand once the rc is verified.

Both axes are locked to a single version number. The PyPI release sets that
number (it owns `[project].version` in `pyproject.toml`); the image workflow
refuses to publish unless the image tag agrees with it. Cutting the package
release also cuts the rc image tag; the stable image tag is pushed by hand
afterwards.

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

1. Run "Release (cz bump)" to cut `vX.Y.Z` — this sets the version, publishes the
   package, and pushes `image-vX.Y.Z-rc.1` to build the rc images.
2. Verify the rc images.
3. `git tag image-vX.Y.Z && git push origin image-vX.Y.Z` to publish the stable
   images and move `:latest`.

## Downstream data-resource consumers

Tolokaforge ships three bundled data files that external tooling reads at
runtime: `pricing.json`, `model_presets.yaml`, `providers.yaml`. Downstream
consumers (leaderboards, cost analysers, integration scripts) must reach
these through the public accessor API on `tolokaforge.core.model_data`
rather than through raw `importlib.resources` lookups:

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

### Post-cutover

The accessors abstract the resource location. When ADR-0030's cutover
(#938) moves the bundled data to the `tolokaforge-models` wheel, exactly
one line changes — the module-level `_DATA_ROOT` constant in
[`tolokaforge/core/model_data.py`](../tolokaforge/core/model_data.py)
flips from `tolokaforge.core.data` to `tolokaforge_models.data`. The
three accessor bodies do not change. Consumer code stays unchanged: the
return type is still a `Path`, the `FileNotFoundError` semantics are
still the same, and the compat guarantee still applies.

See [ADR-0030 § "Downstream data-resource consumers"](adr/0030-tolokaforge-models-split.md#downstream-data-resource-consumers-new--widening-revised-2026-08-07)
for the rationale ADR-0030 chose fixing consumers up-front over shipping a
forwarding stub in the engine.

## See also

- [Standalone Runner Guide](STANDALONE_RUNNER.md#published-images) — the published
  image tag axis and how consumers pull the images.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — Conventional Commits, the input the
  automated bump reads.
