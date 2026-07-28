# Standalone stack example drivers

Runnable drivers that take one real trial from a bundled task pack to a graded
`TrialResult` through the four-service standalone stack in
[`../docker-compose.yaml`](../docker-compose.yaml). They package the exact
`docker compose exec … tolokaforge run-trial` exec-wire the stack exposes; the
mental model lives in [`docs/STANDALONE_RUNNER.md`](../../../docs/STANDALONE_RUNNER.md).
On Apple-Silicon (arm64) hosts the stack runs the `linux/amd64` images under
emulation — see the architecture note in that guide's standalone quickstart.

## `drive_one_trial.py` — host-side Python

Serialises the task pack on the host with the public
`tolokaforge.runner.load_task`, copies the pack into the runner container, and
drives `tolokaforge run-trial` over `docker compose exec`.

Host prerequisites:

- `pip install tolokaforge` (for `load_task` + `TaskConfig.model_dump`).
- Docker with the compose plugin.
- A running standalone stack — see the quickstart in
  [`docs/STANDALONE_RUNNER.md`](../../../docs/STANDALONE_RUNNER.md).
- A provider key in [`../.env`](../.env.example). The trial reads the key from
  the runner container's environment (compose injects it from `.env`); the
  driver forwards no secrets.

Run it from this directory once the stack is up:

```bash
python drive_one_trial.py
```

The agent model defaults to OpenRouter's `anthropic/claude-sonnet-4.6`. If you
set a different provider key in `../.env`, point the driver at a matching model:

```bash
TOLOKAFORGE_EXAMPLE_PROVIDER=anthropic TOLOKAFORGE_EXAMPLE_MODEL=claude-sonnet-4.6 \
    python drive_one_trial.py
```

## `drive_one_trial.sh` — any language (POSIX shell)

The same trial, driven with no host tolokaforge install. It serialises the task
*inside* the runner via the public `load_task`, builds the `start` envelope with
`jq`, and drives `tolokaforge run-trial` over the same exec wire.

Host prerequisites:

- Docker with the compose plugin, and `jq` — nothing else.
- A running standalone stack and a provider key in [`../.env`](../.env.example),
  same as the Python driver.

Run it from this directory once the stack is up:

```bash
sh drive_one_trial.sh
```

It honours the same `TOLOKAFORGE_EXAMPLE_PROVIDER` / `TOLOKAFORGE_EXAMPLE_MODEL`
overrides. There is no `grpcurl` path: the runner gRPC exposes no whole-trial
RPC, so the `run-trial` exec wire is the any-language surface.

## Image tag: local vs. published

The drivers operate on the already-running stack, so they inherit whichever
images `docker compose up` started. `TOLOKAFORGE_IMAGE_TAG` (read by the compose
recipe, defaulting to `latest`) selects those: `latest` (or a pinned release)
pulls published `tolokasoft1/tolokaforge-*` images from Docker Hub, while
`local` runs a developer's locally-built `:local` images. Set it when you bring
the stack up, not when you run a driver.
