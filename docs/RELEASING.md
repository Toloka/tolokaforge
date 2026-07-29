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

## See also

- [Standalone Runner Guide](STANDALONE_RUNNER.md#published-images) — the published
  image tag axis and how consumers pull the images.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — Conventional Commits, the input the
  automated bump reads.
