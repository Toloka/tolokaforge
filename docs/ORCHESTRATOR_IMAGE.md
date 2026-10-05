# Orchestrator Image

The orchestrator image runs a whole `tolokaforge run` batch from a single
container. It is built for the "clean machine" case: a host that has only
Docker and some provider credentials, no Python toolchain, no checkout. You
build (or, once published, pull) the image, drop your credentials in a `.env`,
mount a run config and a task pack, run one command, and collect the report
from a mounted output directory.

> **Not the single-trial driver.** `deploy/standalone/` is a different artifact:
> it stands up the runner stack to drive **one** any-language trial through the
> runner's gRPC surface. This image runs the **full orchestration loop** — task
> discovery, the pre-run gate, per-trial scheduling, grading, and the run
> report — the same `tolokaforge run` you would invoke from a checkout. Reach
> for `deploy/standalone/` to exercise one trial end to end; reach for this
> image to run a batch.

## How it works

`tolokaforge run` materialises a `docker compose` stack for the environment
services a task needs (db-service, runner, and for the full profile rag-service
+ mock-web) and shells out to `docker compose` to bring them up
(`tolokaforge/core/compose_materialisation.py`). Those child containers must be
**siblings** of the orchestrator on the host daemon, with host-published ports,
so the orchestrator talks to the **host** Docker daemon over a mounted
`/var/run/docker.sock` (Docker-out-of-Docker). The image therefore ships the
Docker CLI and Compose plugin but **no** Docker daemon of its own.

The default install is the engine plus the `[dx]` extra (the CLI front-end).
The native and terminal_bench backends need nothing beyond that. Other adapters
are opt-in at build time (see [Adapter variants](#adapter-variants)).

## 1. Build the image

```bash
make docker-build-orchestrator
# or, directly:
scripts/docker/build-orchestrator.sh -t tolokaforge-orchestrator:local
```

The script builds the engine wheel with `uv build`, stages it (plus the
`tolokaforge_models` sibling source) into a temporary build context, and runs
`docker build`. Publishing a prebuilt image to a registry is a separate
follow-up; until then, build locally.

Smoke the result — this proves the `[dx]` extra resolved and the entrypoint
works:

```bash
docker run --rm tolokaforge-orchestrator:local --help
```

## 2. Create your `.env`

```bash
cp deploy/orchestrator/.env.example .env
# edit .env — set the provider key your task's models route to
```

The `.env` is mounted **read-only** into the container, where the engine's
`SecretManager` reads it via `DotEnvProvider`. Credentials never enter the
image, never a build-arg, never a layer. A keyless `--dry-run` needs no `.env`.

## 3. Run a batch

```bash
scripts/docker/run-orchestrator.sh \
    -i tolokaforge-orchestrator:local \
    -c path/to/run_config.yaml \
    -t path/to/task_pack \
    -o ./out \
    -e .env
```

The wrapper mounts, into the container:

| Host | Container | Mode | Purpose |
| --- | --- | --- | --- |
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw | host daemon for child stacks (DooD) |
| run-config dir | `/work/config` | ro | the run config YAML |
| task-pack dir (`-t`) | `/work/tasks` | ro | the tasks the config selects |
| output dir (`-o`) | `/work/out` | rw | run reports land here |
| `.env` (`-e`) | `/work/.env` | ro | provider credentials |

and runs `tolokaforge run --config /work/config/<config>`.

Point your run config at the mounted paths:

- `evaluation.output_dir: /work/out` — so the report lands in the mounted
  output directory. (There is no `--output-dir` flag; the config decides.)
- project / task paths under `/work/tasks` — so the tasks resolve inside the
  container.

Anything after `--` is forwarded to `tolokaforge run`, e.g. a keyless dry run:

```bash
scripts/docker/run-orchestrator.sh -c path/to/run_config.yaml -- --dry-run
```

When the run finishes, the report is under your `-o` directory on the host.

### The socket group (non-root)

The image runs as a non-root user named `runner`. Reaching the mounted host
socket as non-root needs the socket's group added at run time. On Linux the
wrapper does this for you — `--group-add "$(stat -c %g /var/run/docker.sock)"`.
On **Docker Desktop** (macOS/Windows) the socket is proxied and group-ownership
differs; the `--group-add` is usually unnecessary and the wrapper's probe
simply omits it. If a run fails to reach the daemon, check this first.

## Adapter variants

The default image installs `tolokaforge[dx]` — engine plus CLI. The native and
terminal_bench backends run on that. Other adapters are opt-in:

```bash
scripts/docker/build-orchestrator.sh -e dx,adapters
# or: make docker-build-orchestrator ORCHESTRATOR_EXTRAS=dx,adapters
```

The `adapters` extra pulls the adapter packages from their own distributions.
In an environment where those are not resolvable (offline, or before they are
published), build the adapters you need from source into the context instead,
or stay on the default `[dx]` image, which is fully functional for native and
terminal_bench runs.

## Status

The build + `--help` + keyless `--dry-run` path is exercised by the build
script and the smoke command above. A **full keyed end-to-end** run — real
provider keys, child compose stacks over the mounted socket — is the manual
acceptance step. Note the DooD caveat: because child containers are started by
the **host** daemon, any host-path bind mounts the generated compose stacks
reference must resolve identically on the host and in the orchestrator
container; run from a host path, not a container-only path.
