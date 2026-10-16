# Orchestrator Image

The orchestrator image runs a whole `tolokaforge run` batch from a single
container. It is built for the "clean machine" case: a host that has only
Docker and some provider credentials, no Python toolchain, no checkout. You
build or pull the image, drop your credentials in a `.env`,
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

The default install is the engine plus the `[dx]` extra (the CLI front-end),
which runs the native backend. terminal_bench and the other adapters ship as
separate distributions and are opt-in at build time (see
[Adapter variants](#adapter-variants)).

## 1. Build the image

```bash
make docker-build-orchestrator
# or, directly:
scripts/docker/build-orchestrator.sh -t tolokaforge-orchestrator:local
```

The script builds the engine wheel with `uv build`, stages it (plus the
`tolokaforge_models` sibling source) into a temporary build context, and runs
`docker build`. Released versions are published to
`tolokasoft1/tolokaforge-orchestrator:<tag>` by the image release workflow;
build locally for an unreleased or custom (`EXTRAS`) image.

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

and runs `tolokaforge run --config /work/config/<config> --output-dir /work/out`.

The wrapper passes `--output-dir /work/out`, so the report lands in the mounted
output directory without your run config having to name it. The flag overrides
`evaluation.output_dir` for the run; outside the wrapper, `tolokaforge run
--output-dir <path>` targets any directory, and omitting it keeps the config's
`evaluation.output_dir` (or the `results/run_<timestamp>` default).

Point your run config's project / task paths under `/work/tasks` so the tasks
resolve inside the container.

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

## Adapters — lean base, add only what a run needs

The default image installs `tolokaforge[dx]` — engine plus CLI — and bundles
**no** harness adapters: it runs the native backend. A run that needs an adapter
adds just that one, two ways, neither of which rebuilds the image:

- **Mount adapter wheels** — the wrapper's `-A` points at a directory of adapter
  `*.whl` files, and the entrypoint installs them into the image's venv before
  the run. This is the zero-index path: it works for an adapter that is not on a
  public index, because you supply the wheel.

  ```bash
  scripts/docker/run-orchestrator.sh \
      -c config/run.yaml -t ./tasks -o ./out \
      -A ./adapter-wheels          # every *.whl here is installed at start
  ```

- **Name package specs** — set `TOLOKAFORGE_ADAPTERS` to a whitespace-separated
  list of requirement specifiers for adapters that live on an index (honoring
  pip's `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL`):

  ```bash
  EXTRA_DOCKER_ARGS='-e TOLOKAFORGE_ADAPTERS=tolokaforge-adapter-terminal-bench==1.2.0' \
    scripts/docker/run-orchestrator.sh -c config/run.yaml -t ./tasks -o ./out
  ```

An adapter registers its `tolokaforge.adapters` entry point from installed
distribution metadata, so it must be installed — what the entrypoint does — not
merely on `PYTHONPATH`. Dependencies resolve from the configured index (PyPI by
default); for a fully offline run, mount the dependency wheels alongside the
adapter. With neither channel set, nothing installs and the image stays exactly
the native base.

To **bake** a fixed adapter set into the image instead (no install at start),
build with the extras:

```bash
scripts/docker/build-orchestrator.sh -e dx,adapters
# or: make docker-build-orchestrator ORCHESTRATOR_EXTRAS=dx,adapters
```

### Adapters that drive their own Docker sandbox

A delegated adapter may build its own Docker sandbox inside the agent container
through the mounted socket (Docker-out-of-Docker, two levels deep). The wrapper
already mounts the socket, so no extra flag is needed, but three DooD realities
apply:

- **A host-visible staging path.** Any directory the adapter bind-mounts into
  its sandbox through the host daemon must resolve on the **host**, not be a
  container-only path like `/tmp` — point it under the mounted output dir.
- **The model call can originate on the host.** When the sandbox is built on the
  host daemon, the provider call can leave from the **host**, so host
  name-resolution and egress to the provider must work, not just the trial
  network's.
- **The host needs `docker buildx`** when the adapter builds an image for its
  sandbox (`docker buildx version` should succeed).

## Limitations

Because child containers are started by the **host** daemon (Docker-out-of-Docker),
any host-path bind mounts the generated compose stacks reference must resolve
identically on the host and in the orchestrator container — run from a host path,
not a container-only path.
