# All-in-one tolokaforge (single container)

`tolokasoft1/tolokaforge-standalone` runs the whole runner stack — db-service
(8000), rag-service (8001), mock-web (8080), and the runner gRPC service
(50051) — inside **one** container, supervised by `supervisord`. It is the
"runs anywhere Docker does" counterpart to the five-container
[`deploy/standalone/`](../standalone/) compose stack; grading runs in-process on
the runner (aggregate path, [ADR-0040](../../docs/adr/0040-standalone-grader.md)).
See [ADR-0056](../../docs/adr/0056-all-in-one-image.md).

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

The image sets `PIP_CONSTRAINT` and `PIP_BUILD_CONSTRAINT` to
`/opt/tolokaforge/image-constraints.txt`, the `uv.lock` export of the release it
was built from, so a derived image's `pip install` pins every package the lock
lists to the version the engine was tested with. A package the lock does not list
still resolves live, and `uv pip` reads neither variable. To install outside the
lock, clear both for that one command:

```dockerfile
RUN PIP_CONSTRAINT= PIP_BUILD_CONSTRAINT= /opt/venv/bin/pip install 'some-package==1.2.3'
```

Or bake them at build time with the Dockerfile's build args
(`INSTALL_BROWSER=true`, `INSTALL_DOCKER_CLI=true`, or a custom comma-separated
`TOLOKAFORGE_EXTRAS`). Every extra — `otel`, `adapters`, `terminal_bench`,
`office`, `bundle-store-s3`, `all` — builds pinned to the lock, and the
first-party wheels an extra requires (the langfuse plugin for `otel`, the
terminal-bench adapter for `adapters`, `terminal_bench` and `all`) are built from
the image's own source, never pulled from PyPI. `browser` installs the Python
package only; Chromium needs `INSTALL_BROWSER=true`. Build locally with
`make docker-build-standalone`.

### TypeSense flavor (search-backed tasks)

Tasks whose search plane is TypeSense (e.g. the ots packs' `frozen_mcp_core`
adapter) need a TypeSense server. Build with `--build-arg INSTALL_TYPESENSE=true`
to bundle TypeSense as a co-located supervised process (same version the
distributed stack uses). The entrypoint wires `TYPESENSE_HOST=localhost` /
`TYPESENSE_PORT=8108` for the in-container runner and defaults
`TYPESENSE_API_KEY` if unset. A generated key is **printed to the container log**
(a secret in `docker logs`) so you can copy it to the orchestrator — real
deployments should pass their own `TYPESENSE_API_KEY` via `-e` instead.

Run it with 8108 published and the **same** key the orchestrator will use:

```bash
docker run -d --name tolokaforge -p 50051:50051 -p 8108:8108 \
  -e LLM_PROXY_BASE_URL=... -e LLM_PROXY_API_KEY=... \
  -e TYPESENSE_API_KEY="$TS_KEY" \
  tolokasoft1/tolokaforge-standalone:<ver>   # built with INSTALL_TYPESENSE=true
```

The host orchestrator indexes into the published `localhost:8108` and the
in-container runner queries the same server over loopback — one shared instance.
Point the run config at it with `typesense.mode: remote`:

```yaml
orchestrator:
  auto_start_services: false   # attach to the running container (EXECUTOR_ADDRESS=localhost:50051)
  typesense:
    mode: remote
    host: localhost
    port: 8108
    api_key: "${TYPESENSE_API_KEY}"   # same key passed to the container
```

**TypeSense needs the native arch** — build/run the matching `linux/arm64` or
`linux/amd64` variant; its raft writes stall under qemu emulation.

## Notes

- Published **multi-arch** (`linux/amd64` + `linux/arm64`) — one manifest list
  that runs natively on Linux (both arches, incl. Graviton), Apple-Silicon and
  Intel macOS, and Windows/WSL2, with no emulation. (The five scale-out images
  stay `linux/amd64`-only.) A bundled TypeSense flavor in particular needs the
  native arch — its raft writes do not make progress under qemu emulation.
- The container's pid 1 (`supervisord`) runs as root only to add loopback
  `/etc/hosts` aliases (`json-db`, `mock-web`, `rag-service`) so builtin tools
  that address those hostnames work in one container; every service process runs
  as the non-root `runner` user.
- **Release cadence:** new `standalone` tags track **minor/major** releases
  (`:X.Y.0`); patch releases don't republish it (the scale-out images do). A
  maintainer can publish it off-cycle at any version — see
  [docs/RELEASING.md](../../docs/RELEASING.md) § Standalone publish cadence.
- For independent scaling of components, use the five-container
  [`deploy/standalone/`](../standalone/) stack and the detached
  [`tolokaforge-grader`](../../docs/adr/0038-grader-detachment.md) image instead.
