# 0031. Wheel consumers pull published images by default — `docker.image_source` policy

- **Status:** Proposed
- **Date:** 2026-08-13
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`tolokaforge run` today builds all four first-party service images
(`runner`, `db-service`, `rag-service`, `mock-web`) **locally from
source**, regardless of whether the engine is running from a source
checkout or from `pip install tolokaforge`. On a fresh host the first
run pays 2–5 minutes for a docker build (BuildKit setup, ~2 GB of base
layers, wheel install, subset-build stage) and requires a working local
Docker build environment.

Post-[ADR-0030](0030-tolokaforge-models-split.md) (shipped as v0.18.0),
byte-equivalent, rc-smoke-validated images already exist on Docker Hub
at `tolokasoft1/tolokaforge-<svc>:X.Y.Z`. They are what
[ADR-0023](0023-runner-image-internals.md)'s tag axis commits to; they
are what `publish-images.yml` produces from every green release. A wheel
consumer who could pull instead of build would replace a 2–5 min build
with a 30–60 s pull — the same content the release pipeline already
validated, in a fraction of the wall time and with no local Docker
build environment required.

The gap:

- **The engine has no pull code path.** `Image.build()` is build-only;
  there is no `Image.pull()`. The `use_prebuilt_image=True` branch used
  by `dind` / `typesense` stubs an `Image` record and lets Docker
  implicit-pull at `docker run` time — failures then surface at
  container-start, not at provisioning.
- **The image resolution has no policy seam.** `EngineStack.build_
  images()` unconditionally calls into `_build_one_image` which
  unconditionally builds. There is no config surface that lets a
  wheel-installed consumer say "prefer pull" without editing engine
  code.
- **The consumer mix is bimodal.** Repo contributors editing
  `runner.Dockerfile` need to see their edits locally — they must
  build. Wheel consumers, arena / task drivers, downstream OSS
  consumers, and CI-on-a-checkout each want a different default. The
  right shape is a knob, not a hard-coded rule.
- **Docker Hub rate-limits anonymous pulls** at 100 per 6 h per IP.
  A shared CI runner or arena host driving many `tolokaforge run`
  invocations from cold hosts would hit the ceiling and fail — an
  invisible-until-triggered production hazard for the default that
  needs an operator-actionable escape.

The public [`Toloka/tolokaforge#1068`](https://github.com/Toloka/tolokaforge/issues/1068)
ticket named the direction; this ADR captures the shape decision so
future work has a reference for what was rejected and why.

## Decision Drivers

- **Preserve the "one PyPI wheel" story.**
  [ADR-0025](0025-runner-wheel-split.md) already committed the engine to
  a single published wheel. This ADR must not add a new PyPI presence
  or new consumer package. All changes stay inside the engine wheel;
  the pull target is the same Docker Hub image
  [ADR-0023](0023-runner-image-internals.md) already ships.
- **First-run cost is a user-experience contract.** A new user who types
  `pip install tolokaforge; tolokaforge run` should not pay 2–5 min of
  local Docker build for content the release pipeline already
  produced. Reducing that to a 30–60 s pull is the load-bearing win of
  this change.
- **Source-checkout contributors must keep the build loop.** Any
  contributor editing `runner.Dockerfile` or a service's Python has to
  see their edits materialise into the running image. The wheel-vs-
  source discriminator (`(repo_root() / "pyproject.toml").is_file()`)
  is the same one [ADR-0025](0025-runner-wheel-split.md)'s
  `_runner_definition` already uses; reusing it here keeps the shape
  familiar.
- **Rate-limit and platform edge cases have to be actionable, not
  silent.** Any failure mode the policy introduces must surface a
  message an operator can act on (add auth for 429; fall back to
  build for 404 / network / arm64-only host) — not a generic stack
  trace that requires a debugger to interpret.
- **Explicit "pull-or-die" mode exists for consumers who need
  determinism.** Arena / high-volume drivers cannot afford a silent
  rebuild if the pull fails. A three-value knob (not a boolean) is
  necessary because the auto/pull/build distinction has three
  behaviours, not two.
- **Reversibility.** If the pull-vs-build policy proves not worth its
  maintenance overhead, every consumer can opt back into
  `docker.image_source: build` and the engine reverts to a build-only
  default with no PyPI or Docker Hub churn.

## Considered Options

**Policy shape — how does a consumer say pull or build?**

1. **Status quo — always build.** No config knob, no pull path. Rejected: 2–5 min tax on every fresh wheel-consumer host, forever.
2. **Always pull.** Breaks source-checkout contributors — every Dockerfile edit needs an escape hatch. Rejected.
3. **Boolean knob (`docker.prefer_published: bool`).** Two values (true / false) cover pull-vs-build but not "auto based on install shape". A third state (`auto`) is needed for the default to work without every operator touching config. Rejected as a shape.
4. **Tri-valued `docker.image_source: auto | pull | build`.** `auto` picks based on install shape (wheel → pull, source → build); `pull` and `build` are explicit escape hatches. Chosen — see § Decision.
5. **Digest-pinning via a bundled manifest (`sha256:…`).** Supply-chain-clean; immune to `:X.Y.Z` retag drift. Rejected for this ADR — higher implementation cost, no current requirement, would supersede this ADR rather than layer on it.

**Fallback semantics — what happens when pull fails?**

1. **Silent fallback to build on any failure.** Hides the real problem (bad Docker Hub credentials, mirror not configured, network partition). Rejected.
2. **Log-loud fallback in `auto` mode, hard-fail in `pull` mode.** `auto` treats the fallback as best-effort, with a warning naming the failure kind so the operator can act. `pull` refuses to build — the whole point of the explicit knob is "pull or die". Chosen.
3. **Retry-only, no fallback.** Makes rate-limit conditions permanently fatal for the auto default. Rejected.

**Failure-kind sub-classification — how much detail?**

1. **Single `ImagePullError`.** Silent about *why*. Operators can't distinguish "wrong version tag" from "add auth". Rejected.
2. **Three-way kind: `tag_missing` / `rate_limited` / `unreachable`.** Each carries the actionable hint (rate-limit names auth as the fix; tag_missing names the version). Chosen.

**Platform binding — where does `linux/amd64` live?**

1. **Hardcoded in `EngineStack._maybe_pull_service_image`.** Works today; blocks any future arm64-native image without a stack.py edit. Rejected.
2. **Per-service field on `ServiceDefinition` (`published_image_platform`).** Each service declares its published platform; the pull path forwards it verbatim. A future multi-arch or arm64-native image is a per-service edit. Chosen.

## Decision

We adopt **Policy shape 4** (`docker.image_source: auto | pull | build`),
**Fallback semantics 2** (log-loud auto fallback, hard-fail pull),
**Failure-kind sub-classification 2** (three kinds), and
**Platform binding 2** (per-service `published_image_platform`).

### The knob

`DockerConfig.image_source: Literal["auto", "pull", "build"]` with
default `"auto"`, wired into `RunConfig.docker: DockerConfig | None`.
Precedence for override: `--image-source` CLI flag >
`TOLOKAFORGE_IMAGE_SOURCE` env > `docker.image_source` in YAML >
default. The Click `Choice` and the env-var check derive the allow-list
from `typing.get_args(ImageSource)` so the tri-valued literal is a
single source of truth.

### `auto` mode resolution

`resolve_image_source(request, is_wheel_install, engine_version)` is a
pure function; the caller passes the two facts it needs:

- `is_wheel_install`: `not (repo_root() / "pyproject.toml").is_file()`
  (the same discriminator [ADR-0025](0025-runner-wheel-split.md) uses).
- `engine_version`: `tolokaforge.__version__`, which reads
  `importlib.metadata.version("tolokaforge")` with the sentinel
  `"0.0.0+unknown"` when metadata is missing.

Rules:

- `pull` and `build` requests are terminal — the escape hatch always wins.
- `auto` resolves to `build` when `is_wheel_install is False` (source
  checkout).
- `auto` resolves to `build` when the version is the unknown sentinel
  (no valid pull tag).
- `auto` resolves to `build` when the version carries a PEP 440 local
  segment (`+…`) — Docker tags don't accept `+`, so the pull would
  fail with a misclassified error; a local-version wheel means "not
  what's on Docker Hub" by definition.
- Otherwise `auto` resolves to `pull`.

### The pull path

`Image.pull(name, tag, platform=None, log_label=None, client=None)`
mirrors `Image.build()`:

- Cache-hit short-circuit via `_find_existing_image` — but the cache-hit
  branch verifies the cached image's `Os`/`Architecture` match the
  requested platform, re-pulling on mismatch. A wrong-arch cached tag
  (from an operator's `docker tag` or a stale intermediate state) would
  otherwise return an image that fails far downstream with `exec format
  error`.
- Tenacity retry (5 attempts, exponential 10–60 s), with a selective
  `_is_transient` predicate: 404 and 429 are terminal (no retry — a
  retry doesn't change the answer); `ConnectionError` / `TimeoutError` /
  `OSError` retry the full budget (network flap); bare `DockerException`
  is terminal (a "daemon socket closed" doesn't get better by waiting).
- `ImagePullError` sub-classifies failures: `tag_missing` (404),
  `rate_limited` (429, carries response headers so callers can surface
  `Retry-After`), `unreachable` (everything else). The `rate_limited`
  message names authenticated pulls as the fix.
- `log_label` (defaulting to the service name) prefixes the retry log
  so concurrent-retry lines from multiple services can be correlated.

### The stack-side wiring

`ServiceDefinition` grows two fields:

- `published_image_repo: str | None` (default `None`) — the Docker Hub
  repository the pull target resolves against. First-party services in
  `stacks/core.py` and `stacks/full.py` set this to
  `tolokasoft1/tolokaforge-<svc>`. Task-declared services and
  third-party images (`dind`, `typesense`) leave it `None` and take the
  build path.
- `published_image_platform: str` (default `"linux/amd64"`) — the
  platform passed to `Image.pull`. `publish-images.yml` currently
  produces `linux/amd64`-only manifests, so the default matches. A
  future arm64-native or multi-arch image sets this per-service — no
  stack.py edit.

`EngineStack._maybe_pull_service_image` runs before the build path in
`_build_one_image`. It resolves the policy, attempts the pull, and
either returns the pulled image (short-circuiting the build) or falls
through. `use_prebuilt_image=True` (dind / typesense) still
short-circuits *before* the pull policy — third-party images are
unaffected.

In explicit `pull` mode two contract corners raise instead of silently
building:

- `force=True` on `build_images(force=True)` (a caller asking for a
  fresh build) conflicts with "pull or die" — raise so the operator
  sees the conflict.
- `published_image_repo=None` on a service reached in `pull` mode is
  unpullable — raise so the misconfiguration surfaces.

### The orchestrator surfacing

`Orchestrator.run()`'s auto-start block catches `ImagePullError`
specifically and logs `kind` / `image` / `message` / `retry_after`
before re-raising, so the operator sees the actionable hint (e.g.
"configure Docker Hub authenticated pulls") rather than a generic
run-start crash trace.

## Consequences

### Positive

- **First-run cost drops from 2–5 min to 30–60 s** for wheel consumers
  on fresh hosts — the load-bearing win.
- **rc-smoke-validated bytes ship to every wheel consumer** on the
  default path. Local rebuilds can drift (base-image tag resolution,
  transitive deps, buildkit re-serialization); pulling avoids that
  entire class of surprise.
- **No new PyPI presence.** [ADR-0025](0025-runner-wheel-split.md)'s
  "one PyPI wheel" constraint is preserved; the pull target is the
  same Docker Hub image [ADR-0023](0023-runner-image-internals.md)
  already ships.
- **Contributor loop is unchanged.** Source-checkout users still
  build; every Dockerfile / service-code edit still materialises via
  the same `Image.build` path.
- **Explicit modes exist for consumers who need them.**
  `image_source: pull` gives arena / high-volume consumers a "pull or
  die" guarantee; `image_source: build` gives air-gapped operators a
  Docker-Hub-untouching path without editing engine code.
- **Failure modes are operator-actionable.** Rate-limit hint names
  auth; tag-missing hint names the version; unreachable names the
  network error verbatim.

### Negative / Trade-offs

- **Docker Hub rate-limit exposure.** Shared CI runners or arena hosts
  driving many `tolokaforge run` invocations from cold hosts will
  eventually hit the anonymous 100-per-6h ceiling. The mitigation is
  documented (configure auth via `~/.docker/config.json` or switch
  to `image_source: build`), and `auto` mode falls back with a
  loud warning naming rate-limit specifically — but the exposure is
  new.
- **Byte-identity is a *release-pipeline* property, not a *code*
  property.** Wheel consumers now get bytes produced by
  `publish-images.yml`, which may differ layer-for-layer from a local
  build even at the same source-tree revision (base-image drift,
  transitive dep resolution, buildkit re-serialization). For the vast
  majority of consumers this is the *stronger* property (rc-smoke
  validated it), but it is different from "your `docker build` locally
  produces the same digest".
- **Two new fields on `ServiceDefinition`.** `published_image_repo`
  and `published_image_platform` grow the ServiceDefinition surface.
  Both are optional with sensible defaults; third-party
  `ServiceDefinition`s that don't set them take the build path
  unchanged.
- **The `use_prebuilt_image` branch is now the least-preferred of
  three image-resolution paths.** Third-party services (dind,
  typesense) still take it; it just isn't the model first-party
  services follow.
- **Digest-pinning is not delivered here.** Consumers pin by tag; a
  Docker Hub retag would change the underlying bytes without a
  version bump. If a supply-chain requirement lands, a follow-up ADR
  layers digest-pinning on top of this seam.

### Follow-ups

- **Code changes required:** all shipped in the PR that closes
  [Toloka/tolokaforge#1068](https://github.com/Toloka/tolokaforge/issues/1068).
- **Documentation to update:** `docs/RUNNER.md` gains the new default
  and knob (done in the same PR); `docs/RELEASING.md` should name the
  pull-by-default behaviour so release notes call it out
  (follow-up); `docs/STANDALONE_RUNNER.md` untouched.
- **Tests to add:** unit and integration coverage lands in the same
  PR — see `tests/unit/test_image_pull.py`,
  `tests/unit/test_image_source_policy.py`,
  `tests/unit/test_stack_pull_vs_build.py`,
  `tests/unit/test_stack_container_reuse_tag_ordering.py`,
  `tests/unit/test_orchestrator_docker_config_forwarding.py`,
  `tests/canonical/test_wheel_install_runner_context.py` (extended),
  and `tests/integration/deploy/test_run_pull_default.py`.
- **Digest-pinning ADR** (future): if a supply-chain requirement
  emerges, layer digest-pinning on top of this seam via a bundled
  manifest.
- **`docker.pull_credentials` field** (future): programmatic Docker
  Hub auth for arena / task drivers that can't rely on the daemon's
  `~/.docker/config.json`.
- **Multi-arch publish** (future, tracked separately): if / when
  `publish-images.yml` grows an arm64 target, individual services
  override `published_image_platform` per-service — no ADR update
  needed at that time.

## Links

- Related ADRs:
  - [ADR-0022](0022-runtime-independence.md) — the "same package,
    same wheel" clause this ADR preserves at the PyPI level.
  - [ADR-0023](0023-runner-image-internals.md) — the image name / tag
    axis this ADR pulls from.
  - [ADR-0025](0025-runner-wheel-split.md) — the wheel-vs-source
    discriminator this ADR reuses; the one-PyPI-wheel constraint this
    ADR preserves.
  - [ADR-0030](0030-tolokaforge-models-split.md) — the M29 delivery
    (v0.18.0) that made the published images available in the shape
    this ADR relies on.
- Related code:
  - `tolokaforge/core/models/docker_config.py` — `DockerConfig.image_source`,
    `ImageSource` literal.
  - `tolokaforge/docker/image.py` — `Image.pull`, `ImagePullError`,
    `_cached_image_matches_platform`.
  - `tolokaforge/docker/image_source_policy.py` —
    `resolve_image_source` policy helper.
  - `tolokaforge/docker/stack.py` — `EngineStack._maybe_pull_service_
    image`, `ServiceDefinition.published_image_repo`,
    `published_image_platform`.
  - `tolokaforge/dx/cli/main.py` — `--image-source` flag,
    `TOLOKAFORGE_IMAGE_SOURCE` env, precedence.
- External references:
  - [Toloka/tolokaforge#1068](https://github.com/Toloka/tolokaforge/issues/1068)
    — public umbrella issue.
  - [Toloka/tolokaforge#1082](https://github.com/Toloka/tolokaforge/pull/1082)
    — implementing PR.
