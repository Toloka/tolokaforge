# 0053. All-in-one (single-image, multi-service) `tolokaforge-standalone` image

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Realizes:** [ADR-0040](0040-standalone-grader.md) § aggregate image; builds on [ADR-0023](0023-runner-image-internals.md)/[ADR-0024](0024-container-command-surface.md) (image name+command contract) and [ADR-0021](0021-component-monitoring-seam.md) (component kinds)

## Context and Problem Statement

Tolokaforge publishes five per-component images
(`tolokasoft1/tolokaforge-{runner,db-service,rag-service,mock-web,grader}`) and a
`deploy/standalone/docker-compose.yaml` that wires them on one network. That
shape is correct for scaling components independently, but it forces every
evaluator — including someone who just wants to try the harness on a laptop, a
CI runner, or a restricted environment — to orchestrate five containers.

We want a second, complementary packaging: one image that runs the whole runner
stack in a single container, so it "just runs" anywhere Docker does, without
giving up the distributed shape.

Terminology: this is the **all-in-one** / **single-image multi-service** image.
It is deliberately NOT called a "monolith" — ADR-0023/0025 already use
"monolithic" for the runner image's full-wheel *internal* composition, a
different axis.

## Decision Drivers

- **No engine changes.** The five services are self-contained Python/uvicorn +
  gRPC processes with no mandatory external datastore (JSON DB, RAG, and
  mock-web are in-process FastAPI apps; Postgres/Redis/S3/TypeSense are
  task-specific or scale-out concerns). Inter-service addressing is env-var
  driven and localhost-friendly, and an attach mode already exists
  (`orchestrator.auto_start_services: false` + `EXECUTOR_ADDRESS`). The
  collapse is packaging, a supervisor, and configuration — not new runtime code.
- **Collapse transport, not seams.** Grading stays behind the `GradingSubstrate`
  seam; the all-in-one image selects the aggregate path (ADR-0040).
- **One shared install.** A single `/opt/venv` serving every process is the
  all-in-one image's core advantage over five separate images.
- **Add-ons like PyPI extras.** The lean base must stay lean; heavier surfaces
  are opt-in, added the same way `pip install 'tolokaforge[browser]'` works.

## Decision

Add a sixth first-party image, `tolokasoft1/tolokaforge-standalone`, built from
`tolokaforge/docker/dockerfiles/standalone.Dockerfile`.

- **Processes.** `supervisord` (pid 1) runs db-service (8000), rag-service
  (8001), mock-web (8080), and the runner (`python -m tolokaforge.runner`,
  50051), each as the non-root `runner` user, all on loopback
  (`DB_SERVICE_URL`/`RAG_SERVICE_URL` → `http://localhost:…`). The builtin
  `http_request`/`browser`/`rag_search` tools address `mock-web:8080`,
  `rag-service:8001`, `json-db:8000` by bare hostname, so the entrypoint
  (running as root) adds those names to `/etc/hosts` as loopback aliases before
  handing off; only `/etc/hosts` setup needs root, the workloads do not.
- **Grading.** In-process via the aggregate path (`grader.name: runner_rpc` +
  `InProcessGradingSubstrate`, ADR-0040) — no separate grader process. The
  detached grader stays available via the `tolokaforge-grader` image for
  distributed deployments.
- **Default mode: appliance.** The default command brings the services up and
  stays healthy; evaluators drive trials from the host or `docker exec` against
  `localhost:50051`. A `batch` command brings services up, waits for the
  runner, then runs `tolokaforge run` in-container (requires
  `auto_start_services: false` in the mounted config) and exits.
- **Add-ons.** The lean base installs `tolokaforge[dx,server,rag,runner]` + the
  rag search stack with a **CPU-only torch** (the dominant size win) + a baked
  embedding model. `pip` is retained in the runtime (unlike runner/grader, which
  strip it) so a heavier image is one layer away:
  `FROM tolokasoft1/tolokaforge-standalone:<ver>` + `pip install 'tolokaforge[browser]'`.
  Build-arg toggles `TOLOKAFORGE_EXTRAS`, `INSTALL_BROWSER`, `INSTALL_DOCKER_CLI`
  bake the common add-ons at build time (same pattern as `runner.Dockerfile`).
- **Build + release.** Registered in `tolokaforge/docker/builder.py`
  (`_standalone_definition()`, built on demand via `tolokaforge docker build
  --service standalone` / `make docker-build-standalone`, never in the default
  `build_all_images` sweep). Added to `publish-images.yml`'s
  `FIRST_PARTY_IMAGES` + one matrix row, inheriting the OIDC push, rc→stable
  auto-promotion, and `:X.Y.Z`/`:X.Y`/`:latest` tagging; `linux/amd64` only. A
  keyless rc-smoke (`tests/integration/deploy/test_standalone_image_smoke.py`)
  gates `:latest` promotion.

The image name + command surface is the stability commitment (ADR-0023/0024);
its internal process layout is not.

## Consequences

- **Positive.** One-container deployment; one shared venv; full task coverage in
  the lean base except browser/coding (opt-in); the detached/scale-out shape is
  unchanged.
- **Negative / trade-offs.** `supervisord` is a new dependency, and pid 1 runs as
  root to set `/etc/hosts` aliases (service workloads run as `runner`). The
  all-in-one image is the heaviest build, so it is on-demand only. Running the
  full orchestrator in-container (batch mode) is newly exercised — previously
  only `run-trial`/`worker` ran in-container.
- **Follow-ups.** A `-full` flavor tag (browser + docker-cli baked) if demand
  warrants; surfacing the co-located services through the ADR-0021 `process`
  `ComponentKind` when driven in-container.
