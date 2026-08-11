# tolokaforge-adapter-terminal-bench

Runs terminal-bench task packs on the tolokaforge engine.

## Environment contract

Terminal-bench tasks author a `docker-compose.yaml` that references
`T_BENCH_*` variables (plus `CPUS` / `MEMORY`) which terminal-bench's own
provisioner injects at up-time. The tolokaforge engine never sets those, so
the compose file is **synthesised** before provisioning — the adapter emits
a self-contained compose file the engine can bring up unchanged, alongside
a staging directory that carries the task's build context, tests, and log
mountpoints.

### Staging directory

`compose_synthesis.materialise_task_environment` writes each task's
materialised environment to
`{staging_root}/{task_id}-{digest}`, where `digest` is a content hash over
the task directory and the synthesis parameters. Two calls with the same
inputs resolve to the same directory — synthesis is idempotent.

Contents of a staging directory:

- A copy of the task pack (excluding `__pycache__`).
- `tests/test.sh` — normalised: when the task ships `run-tests.sh` at its
  root and no `tests/test.sh`, the root script is promoted to
  `tests/test.sh`; an existing `tests/test.sh` wins.
- `_logs/verifier/` and `_logs/agent/`, created empty. The per-trial
  compose context copy preserves them, so the agent-service log volumes
  find mountpoints.
- `docker-compose.tolokaforge.yaml` — the synthesised compose file.

### Synthesised compose contract

- Every service declared by the task is preserved. The adapter-owned
  variable set is **resolved at synthesis time** so no `${T_BENCH_*}`,
  `${CPUS}`, or `${MEMORY}` survives in the emitted file:

  | Variable                                    | Resolved value                                                                                            |
  | ------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
  | `T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME`     | `tbench-{task_id}:{image_tag}` (or `{image_registry}/{task_id}:{image_tag}` when `image_registry` is set) |
  | `T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME` | `tbench_${TOLOKAFORGE_TRIAL_SLUG}_{agent_service}`                                                        |
  | `T_BENCH_CONTAINER_LOGS_PATH`               | `/logs`                                                                                                   |
  | `T_BENCH_TASK_LOGS_PATH`                    | `./_logs`                                                                                                 |
  | `T_BENCH_CONTAINER_AGENT_LOGS_PATH`         | `/logs/agent`                                                                                             |
  | `T_BENCH_TASK_AGENT_LOGS_PATH`              | `./_logs/agent`                                                                                           |
  | `T_BENCH_TEST_DIR`                          | `/tests`                                                                                                  |
  | `CPUS`                                      | `str(meta.cpus)`                                                                                          |
  | `MEMORY`                                    | `{meta.memory_mb}M`                                                                                       |

  `${TOLOKAFORGE_TRIAL_SLUG}` is the one variable that survives into the
  emitted file — the engine writes it to the per-trial `.env` at provision
  time so each trial's containers get a unique name.

- The **agent service** (`main`, or the sole service when `main` is not
  declared) gets:
  - a pinned `image:` — `tbench-{task_id}:{image_tag}` for local builds
    (with the task's `build:` retained so the orchestrator can build it)
    or `{image_registry}/{task_id}:{image_tag}` when `image_registry` is
    set (with `build:` dropped so the image is pulled);
  - `container_name: tbench_${TOLOKAFORGE_TRIAL_SLUG}_{agent_service}`;
  - `volumes: ["./tests:/tests", "./_logs:/logs"]` — the relative bind
    mounts against the staging dir replace the `T_BENCH_*` log mounts;
  - `TEST_DIR=/tests` in its `environment:`.

- Two engine services are **injected** alongside the task's own:
  - `runner` (default image `tolokaforge-runner:local`) — exposes gRPC on
    `50051`, addresses `db-service` via `DB_SERVICE_URL`, depends on the
    agent service (`service_started`) and `db-service` (`service_healthy`).
  - `db-service` (default image `tolokaforge-db-service:local`) — exposes
    HTTP on `8000` with a `/health` probe.

- Fail-loud rules:
  - A task compose file declaring its own `runner` or `db-service` raises
    `ValueError` naming the collision and the task.
  - A compose file declaring more than one service and none named `main`
    raises `ValueError` naming the task and the declared services.
  - A floating `image_tag` (`latest`, `main`, `master`, `edge`, `stable`,
    `dev`, `develop`, `nightly`, `head`) is rejected — the same rule
    `EnvironmentManifest._check_pinned_images` enforces, applied earlier
    with the adapter's own message.

### Agent-image pre-build

The synthesised compose file references `tbench-{task_id}:{image_tag}` for
the agent service. That image must exist locally before the trial's
provision brings the stack up, otherwise `docker compose up --wait` triggers
a full image build inside the `--wait` window — and two concurrent trials
of the same task both build the same tag.

The adapter declares the build via
`DockerStackRequirements.image_builds` (one
`ComposeImageBuild(compose_file=..., service=agent_service)` per task); the
orchestrator's image-preparation step runs `docker compose build` once per
run before any trial provisions. Synthesis itself never shells out —
`materialise_task_environment` only reads and writes files.
