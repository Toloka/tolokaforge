# All-in-one tolokaforge (single container)

`tolokasoft1/tolokaforge-standalone` runs the whole runner stack — db-service
(8000), rag-service (8001), mock-web (8080), and the runner gRPC service
(50051) — inside **one** container, supervised by `supervisord`. It is the
"runs anywhere Docker does" counterpart to the five-container
[`deploy/standalone/`](../standalone/) compose stack; grading runs in-process on
the runner (aggregate path, [ADR-0040](../../docs/adr/0040-standalone-grader.md)).
See [ADR-0053](../../docs/adr/0053-all-in-one-image.md).

Not "monolith" — that word means the runner image's full-wheel internals in
[ADR-0023/0025](../../docs/adr/0023-runner-image-internals.md).

## Appliance (default): services up, drive from outside

```bash
# Services up and healthy; runner gRPC published on the host.
docker run -d --name tolokaforge \
  -p 50051:50051 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  tolokasoft1/tolokaforge-standalone:latest

docker exec tolokaforge tolokaforge --version
```

Drive trials from the host or `docker exec`, pointing the orchestrator at the
already-running runner (no Docker bring-up):

```bash
# In your run config:
#   orchestrator:
#     auto_start_services: false
EXECUTOR_ADDRESS=localhost:50051 tolokaforge run --config run.yaml
```

Provider keys ride the container env (`-e OPENAI_API_KEY=…`, or the
multi-provider `-e TOLOKAFORGE_SECRETS_JSON=…`); nothing is baked into the image.

## Self-contained one-shot (batch)

Bring the services up, run one batch in-container, and exit with its status. The
mounted run config **must** set `orchestrator.auto_start_services: false`
(`EXECUTOR_ADDRESS` is set for you):

```bash
docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -v "$PWD/work:/work" \
  tolokasoft1/tolokaforge-standalone:latest \
  batch --config /work/run.yaml
```

## Add-ons (like PyPI extras)

The lean base covers JSON-DB, RAG, API, and mock-web tasks. Heavier surfaces are
opt-in — the image keeps `pip`, so adding an extra is one layer:

```dockerfile
FROM tolokasoft1/tolokaforge-standalone:latest
# Browser tasks (Chromium):
RUN /opt/venv/bin/pip install 'tolokaforge[browser]' && \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/venv/bin/playwright install --with-deps chromium
```

Or bake them at build time with the Dockerfile's build args
(`INSTALL_BROWSER=true`, `INSTALL_DOCKER_CLI=true`, or a custom
`TOLOKAFORGE_EXTRAS`). Build locally with `make docker-build-standalone`.

## Notes

- `linux/amd64` only (like every published image); runs under emulation on
  Apple Silicon.
- The container's pid 1 (`supervisord`) runs as root only to add loopback
  `/etc/hosts` aliases (`json-db`, `mock-web`, `rag-service`) so builtin tools
  that address those hostnames work in one container; every service process runs
  as the non-root `runner` user.
- For independent scaling of components, use the five-container
  [`deploy/standalone/`](../standalone/) stack and the detached
  [`tolokaforge-grader`](../../docs/adr/0038-grader-detachment.md) image instead.
