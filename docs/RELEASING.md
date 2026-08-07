# Releasing Tolokaforge

A tolokaforge release has two axes, each with its own GitHub Actions workflow:

- **The PyPI package** — `vX.Y.Z` tags, cut automatically by the "Release (cz
  bump)" workflow.
- **The Docker images** — the `image-vX.Y.Z-rc.1` release-candidate tag is cut
  automatically by the same "Release (cz bump)" workflow; the stable images
  are promoted automatically once the keyless rc-smoke passes. No manual tag
  push is required for the routine release path.

Both axes are locked to a single version number. The PyPI release sets that
number (it owns `[project].version` in `pyproject.toml`); the image workflow
refuses to publish unless the image tag agrees with it. Cutting the package
release cuts the rc image tag; the auto-promote job flips the rc images to
stable in the same run once the smoke gate is green.

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

## Docker images — `image-vX.Y.Z-rc.1` (auto) → auto-promoted to stable

The four first-party images —
`tolokasoft1/tolokaforge-{runner,db-service,rag-service,mock-web}` — are published
by [`publish-images.yml`](../.github/workflows/publish-images.yml), which fires on
any pushed `image-v*` tag. The release workflow cuts the release-candidate tag
`image-vX.Y.Z-rc.1` for you; the stable images are promoted from the rc digests
automatically once the keyless rc-smoke gate passes. The workflow builds the wheel
once; only the images that ship it — `runner` and `rag-service` — are layered from
that artifact, while `db-service` and `mock-web` build without it.

Images are `linux/amd64` only. The standalone compose recipe pins
`platform: ${TOLOKAFORGE_PLATFORM:-linux/amd64}`, so Apple-Silicon hosts run the
published images under emulation.

### Version guard

Before building, the workflow asserts that `image-v<base>` equals the
`pyproject` `[project].version`, with any `-rc.N` suffix stripped from the tag
first. The image tag must therefore match the package version cut by the PyPI
release. The two axes share one version number but are separate tag pushes.

### Release-candidate, then automatic promotion to stable

Every image release goes through a release candidate first, then auto-promotes
to stable when the smoke gate is green. The whole sequence runs in one
`publish-images.yml` invocation off the single rc tag push.

1. **The rc tag is cut for you.** When "Release (cz bump)" cuts `vX.Y.Z`, it also
   pushes `image-vX.Y.Z-rc.1`. That routes through the `pre-stable` deployment
   environment and pushes the immutable `:X.Y.Z-rc.1` tags.

2. **rc-smoke runs against the freshly-pushed images.** The `smoke` job pulls
   each `:X.Y.Z-rc.1` image and asserts entrypoint + healthcheck + the documented
   exec surface (see the "keyless rc-smoke" section below). It is keyless: the
   run-trial check accepts a well-formed `error` wire line as a pass, so this
   gate costs zero tokens and needs no provider key.

3. **Auto-promote fires only on a green smoke.** The `auto-promote-rc-to-stable`
   job runs in the `release` deployment environment, mints its own Docker Hub
   OIDC token, and uses `docker buildx imagetools create` to copy the immutable
   rc digest onto the `:X.Y.Z` stable tag, then onto the moving `:latest` and
   `:X.Y` tags. Same digest, three additional tag references — no rebuild. It
   finally publishes a GitHub Release listing each image's digest.

A red rc-smoke blocks the promote step: `:latest` stays on the previous stable
version, and the operator investigates the rc images before anything moves.

### Override — manual `image-vX.Y.Z` push

The manual path stays available for rare cases the automated path cannot cover
(promoting an older rc after a hotfix, re-publishing after a Docker Hub incident,
etc.). Pushing an `image-vX.Y.Z` tag (no `-rc.` suffix) triggers the same
`publish-images.yml` workflow through the `move-tags` job, which mints the
`release`-environment OIDC token and moves `:latest` / `:X.Y` onto the
already-published immutable `:X.Y.Z` digest. Use this only when the automated
promote is not the right fit; for a routine release it does nothing beyond what
auto-promote already did.

### Dry run

Running `publish-images.yml` from the Actions tab as a `workflow_dispatch` builds
all four images **without** logging in or pushing — a safe pre-check that the
Dockerfiles and wheel still build.

## Typical order

1. Run "Release (cz bump)" from the Actions tab. This cuts `vX.Y.Z` (PyPI
   publishes automatically) and pushes `image-vX.Y.Z-rc.1` (rc images build,
   rc-smoke gates the promote).
2. Watch the `publish-images.yml` run. When `smoke` passes, the
   `auto-promote-rc-to-stable` job flips the rc digests to stable, moves
   `:latest` and `:X.Y`, and publishes the GitHub Release.

That is the entire routine. There is no manual tag push for the happy path.

## See also

- [Standalone Runner Guide](STANDALONE_RUNNER.md#published-images) — the published
  image tag axis and how consumers pull the images.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — Conventional Commits, the input the
  automated bump reads.
