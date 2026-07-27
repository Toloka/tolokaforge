# 0023. Runner image internals — monolithic wheel + `[runner]` extra, internals not a stability commitment

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

M14 publishes the four first-party images to Docker Hub under `docker.io/tolokasoft1/tolokaforge-{runner,db-service,rag-service,mock-web}`, so `docker pull` becomes a supported way to consume the runner. Publishing an image raises a question the in-repo `make docker-build` flow never had to answer: **what does the published runner image guarantee about how it is composed internally?**

Today the runner image is built by [`runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) from the full `tolokaforge` wheel plus its `[runner]` extra — one wheel, one package, the `[runner]` extra ([ADR-0022 §4b](0022-runtime-independence.md#4b--slim-image-budget--policy)) carrying the domain-tool drivers. There is no `tolokaforge-core` / `tolokaforge-runner` split.

A runner-only wheel would be smaller and would decouple the runner's release cadence from the orchestrator's, which is attractive. But it is not free: the runner's runtime closure is ~90% shared with the orchestrator, the adapters, and the CLI. Carving a runner-only wheel first requires extracting a `tolokaforge-core` wheel out of the repository's highest-fan-in code — `core.models`, `core.llm.*`, `secrets`, and the trial/runtime contract — an epic-sized change (~4–6 issues) on exactly the modules every other subsystem imports. Gating the M14 publish on that extraction would block a shippable, useful deliverable (public pull-able images) behind a large, risky refactor.

The forces:

- **M14 needs a publishable image now**, and the current monolithic image is production-usable (390 MB, [ADR-0022 §4b](0022-runtime-independence.md#4b--slim-image-budget--policy)).
- **The wheel carve is real work worth doing**, but it is a milestone of its own, not a rider on the publish workflow.
- **Consumers need a stability promise they can build against** — but that promise must be scoped to what is actually stable, so a future carve is not a broken contract.

## Decision Drivers

- **Ship the smallest honest thing.** M14 delivers pull-able images; it does not owe consumers a particular internal layout.
- **Do not gate a shippable deliverable on a large refactor.** The `tolokaforge-core` extraction touches the highest-fan-in code in the repo and deserves its own milestone-level design and behaviour-lock.
- **Scope the compatibility promise precisely.** A promise that is broader than what is truly stable becomes a broken contract the moment internals change; a promise scoped to the name+tag contract survives a future carve.
- **One package, one wheel** — consistent with [ADR-0022](0022-runtime-independence.md)'s "same package, same wheel; runner independence is a packaging-extra and CLI-mode story, not a repository split."

## Considered Options

1. **Monolithic image, internals uncommitted** — publish the current single-wheel + `[runner]`-extra image; commit only the image name + tag contract as stable; defer the wheel carve to its own milestone.
2. **Carve `tolokaforge-core` first, then publish** — extract a runner-only wheel before M14 ships, so the published image is already the slim carved artifact.
3. **Publish monolithic and commit the internal layout too** — treat the wheel + extra composition itself as part of the published contract.

## Decision

We adopt **Option 1**: the published runner image ships **monolithic** — the base `tolokaforge` wheel installed with its `[runner]` extra, exactly as [`runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) builds it today. No wheel carve, no repository split.

**Key clause — the stability boundary.** The M14 image *internals* are **not** a stability commitment. What is committed and stable is the **published image name + tag contract**: `docker.io/tolokasoft1/tolokaforge-runner:X.Y.Z` (and the coordinated `:X.Y` / `:latest` / `:X.Y.Z-rc.N` tag axis) resolves to a working runner that honours the command surface of [ADR-0024](0024-container-command-surface.md). *How* that image is composed internally — one monolithic wheel today, a carved `tolokaforge-core` + `tolokaforge-runner` pair tomorrow — may change **without** breaking that contract. A consumer that `docker pull`s a pinned tag and talks to the runner over its committed command surface is unaffected by an internal carve; a consumer that reaches into the image's site-packages layout is relying on something that was never promised.

Option 2 is rejected for M14: the `tolokaforge-core` extraction is a milestone-scoped epic (~4–6 issues) on the repo's highest-fan-in modules, too large to gate a publish workflow on. It is deferred to **Milestone 15** (umbrella [#622](https://github.com/Toloka/tolokaforge/issues/622) — runner wheel split / `tolokaforge-core` extraction). Option 3 is rejected because committing the internal layout would make the deferred carve a breaking change, contradicting the whole reason to defer it.

## Consequences

### Positive

- M14 ships pull-able images now, without waiting on the wheel-carve epic.
- The stability promise is scoped to exactly what stays true across a future carve — the name + tag contract — so M15 can re-lay the internals without a breaking-change event for consumers.
- One package, one wheel keeps the build path identical to the developer `make docker-build` flow; the published image and the local `:local` image are composed the same way.

### Negative / Trade-offs

- The published runner image carries the full `tolokaforge` closure (via the `[runner]` extra), not a runner-only subset — larger than a carved image would be. The size is bounded and measured (390 MB, [ADR-0022 §4b](0022-runtime-independence.md#4b--slim-image-budget--policy)); the further reduction a carve would buy is deferred, not lost.
- Runner and orchestrator share one wheel version, so the runner image's version tracks the package version rather than an independent runner cadence — acceptable while both ship from one repository.

### Follow-ups

- Code changes required: none — this ADR records the composition `runner.Dockerfile` already implements.
- Documentation to update: [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md) states the published image is pull-able and that its internal layout is not a committed surface.
- Tests to add: none for this ADR; the publish workflow's rc-smoke locks the *name+tag → working runner* contract this ADR declares stable.
- Deferred work: the `tolokaforge-core` wheel carve is Milestone 15 ([#622](https://github.com/Toloka/tolokaforge/issues/622)).

## Links

- Related ADRs:
  - [ADR-0022](0022-runtime-independence.md) — runtime independence; §4b sets the slim-image budget and the "same package, same wheel" policy this ADR keeps
  - [ADR-0024](0024-container-command-surface.md) — the container command surface that *is* the committed contract behind the stable image tag
- Related code:
  - [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) — the monolithic wheel + `[runner]` extra build this ADR ratifies
- Related issues:
  - [GH #610](https://github.com/Toloka/tolokaforge/issues/610) — Milestone 14 umbrella (runner as a distributable service)
  - [GH #622](https://github.com/Toloka/tolokaforge/issues/622) — Milestone 15 umbrella (runner wheel split / `tolokaforge-core` extraction), where the deferred carve lands
