# Standalone stack example drivers

Runnable drivers that take one real trial from a bundled task pack to a graded
`TrialResult` through the four-service standalone stack in
[`../docker-compose.yaml`](../docker-compose.yaml). They package the exact
`docker compose exec … tolokaforge run-trial` exec-wire the stack exposes; the
mental model lives in [`docs/STANDALONE_RUNNER.md`](../../../docs/STANDALONE_RUNNER.md).

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

## Image tag: local vs. published

The drivers operate on the already-running stack, so they inherit whichever
images `docker compose up` started. `TOLOKAFORGE_IMAGE_TAG` (read by the compose
recipe, defaulting to `latest`) selects those: `latest` (or a pinned release)
pulls published `tolokasoft1/tolokaforge-*` images from Docker Hub, while
`local` runs a developer's locally-built `:local` images. Set it when you bring
the stack up, not when you run a driver.
