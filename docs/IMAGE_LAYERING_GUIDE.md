# Task-Pack Image Layering Guide

A recipe for structuring the Docker images a task pack references so that
Docker's content-addressable layer cache transparently shares build work
across every trial in a task family. The pattern is the 3-tier image
hierarchy — **base → environment → instance** — established by
[SWE-bench](https://github.com/princeton-nlp/SWE-bench) and cited in
[ADR-0009](adr/0009-environment-manifest.md#industry-precedents-studied) as
a build-time optimisation that composes with any pinned-image manifest.

This is a **task-authoring** guide. The engine reads whatever images your
compose file declares; nothing here changes engine behaviour. The gain comes
entirely from how you split your Dockerfiles.

## 1. Why layering

Per-trial isolation means the per-trial runtime backend brings up a fresh
stack for every trial (see
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) and the
[`MULTI_CONTAINER_GUIDE.md`](MULTI_CONTAINER_GUIDE.md)). Isolation is the
point — but a naive task pack pays for it twice: once in image size, once in
build time, multiplied across every task in the family.

There is a tension. Bake everything into one image per task and each task is
self-contained but the family duplicates gigabytes of shared runtime and
dependencies across N images. Share nothing and every trial rebuilds the
world.

Layering resolves the tension by matching image structure to how the content
actually varies:

| Tier | How many images | Rough size | Varies |
| --- | --- | --- | --- |
| **Base** | 1 for the whole family | ~1 GB | Almost never — OS, language runtime, generic tooling. |
| **Environment** | 1 per task family | ~5 GB each | Per family — the shared dependency set. |
| **Instance** | 1 per task | thin layer | Per task — the task-specific bits only. |

Because Docker addresses layers by content hash, the base layer is built and
stored **once** and reused by every environment image; each environment layer
is built once per family and reused by every instance image on top of it.
Only the thin instance layer is unique per task. The shared bytes are
downloaded, built, and cached a single time.

## 2. The three tiers

### Base image

OS plus language runtime plus generic tooling that *every* task family shares
— compilers, a package manager, shells, common CLIs. Built once, referenced
by every environment image across every family. It changes only when you bump
the base OS or toolchain, so its layers stay cache-warm for a long time.

### Environment image

A single task family's shared dependencies, built `FROM` the base image. This
is where the heavy, slow-to-install content lives: the family's library set,
compiled artifacts, seeded datasets, service binaries. Built once per family
and reused by every instance image in that family. When a task family shares a
large dependency closure, this tier is where layering pays off most.

### Instance image

The per-task variant, built `FROM` the environment image. Keep it thin: the
task-specific config, a fixture file, an entrypoint tweak. Everything
expensive already lives in the layers below, so building a new instance image
is fast and adds only a few megabytes on top of the cached environment layer.

## 3. Dockerfile examples

Three Dockerfiles, one per tier. Each `FROM` points at the tier below by a
**pinned** reference (see §4 for why pinning matters).

**Base** — `Dockerfile.base`:

```dockerfile
# Tier 1: OS + language runtime + generic tooling.
# Built once; shared by every task family. Rebuild only on a toolchain bump.
FROM python:3.12-slim-bookworm

# Generic tooling every family needs — not task-specific.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# A package manager pinned at the base so every layer above resolves the same.
RUN pip install --no-cache-dir uv==0.5.11
```

**Environment** — `Dockerfile.env`:

```dockerfile
# Tier 2: one task family's shared dependency closure.
# Built once per family, FROM the pinned base. This is the expensive layer.
FROM registry.example.com/myfamily-base:1.0@sha256:<base-digest>

WORKDIR /app

# The family's shared dependencies. Copy the lockfile first so this whole
# layer is cache-reused whenever the dependency set is unchanged.
COPY requirements.lock ./
RUN uv pip install --system -r requirements.lock

# Shared read-only data the whole family reads — seeded once here, not per task.
COPY family-fixtures/ /opt/family-fixtures/
```

**Instance** — `Dockerfile.instance`:

```dockerfile
# Tier 3: the per-task variant. Thin layer FROM the pinned environment image.
# Only task-specific content lives here; everything heavy is already cached below.
FROM registry.example.com/myfamily-env:1.0@sha256:<env-digest>

# Just the bits that differ per task.
COPY task-0042/config.yaml /app/config.yaml
COPY task-0042/fixture.json /app/fixture.json
```

Build them bottom-up, tagging each with a stable, pinnable reference:

```bash
docker build -f Dockerfile.base     -t registry.example.com/myfamily-base:1.0 .
docker build -f Dockerfile.env      -t registry.example.com/myfamily-env:1.0  .
docker build -f Dockerfile.instance -t registry.example.com/myfamily-task-0042:1.0 .
```

## 4. Referencing layered images in the compose file

A task pack points at an image through the `image:` field of a service in its
`environment.compose.yaml` — the same field for a layered instance image as
for any off-the-shelf image. The manifest validator **rejects floating tags**
(`:latest`, `:main`, `:edge`, and the rest) so that a trial always resolves
the exact same bytes; pin with an immutable tag or a digest.

```yaml
services:
  runner:
    image: tolokaforge-runner:local          # engine runner; :local is a pinned alias
    ports:
      - "50051"

  app-service:
    # The instance image sitting on top of the family's environment layer.
    # Pinned by digest — reproducible, and the cache key Docker keys off.
    image: registry.example.com/myfamily-task-0042:1.0@sha256:<instance-digest>
    ports:
      - "8080"

  app-db:
    image: postgres:16                        # a pinned off-the-shelf image
    ports:
      - "5432"
```

The engine treats `myfamily-task-0042` like any other pinned image: it pulls
it, and Docker's layer cache recognises the base and environment layers it
already holds, so only the thin instance layer moves. See
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md#referencing-the-runner-image-from-task-manifests)
for how the engine's own `runner` / `db-service` images are referenced by the
`:local` pinned alias.

## 5. Cache tuning

SWE-bench exposes a `--cache_level` flag that decides which tiers persist
between runs (discard everything, keep base, keep base + environment, keep
all). The tolokaforge engine needs no such flag: **Docker's own
content-addressable layer cache does the tuning automatically.** A pinned
`FROM` is a stable cache key, so as long as the reference below a layer is
unchanged, Docker reuses the cached layer on the next build or pull without
being told to.

What you control is how *cache-friendly* your Dockerfiles are:

- **Pin every `FROM`.** A digest or immutable tag is a deterministic cache
  key; a floating tag silently invalidates everything above it when the
  upstream moves (and the manifest validator rejects floating tags in the
  compose file for exactly this reason).
- **Order layers by change frequency** — least-volatile first. Copy the
  dependency lockfile and install *before* copying task content, so a
  task-content change doesn't bust the dependency layer.
- **Keep the instance layer thin.** Anything shared across the family belongs
  in the environment image, where it is built once, not in each instance
  image, where it is rebuilt per task.
- **Push the shared tiers to a registry** so a cold host pulls the cached base
  and environment layers instead of rebuilding them.

## 6. When the pattern doesn't apply

Layering is an optimisation for *shared* build cost. Skip it when there is
nothing to share:

- **One-off tasks.** A single task with no family around it has no second
  image to amortise the split against — a single self-contained Dockerfile is
  simpler and just as fast.
- **Tasks with no shared dependencies.** If each task in a "family" installs a
  disjoint dependency set, there is no common environment layer to factor out;
  the environment tier would be empty and the split adds ceremony for no cache
  gain.
- **Small images.** If the whole image is already thin (a slim base plus a
  handful of megabytes), the base/environment/instance split saves little and
  costs a three-Dockerfile maintenance surface.

Reach for the 3-tier pattern when a task family shares a large, slow-to-build
dependency closure across many tasks. That is exactly the case where the
per-trial isolation cost is highest and the cache win is largest.
