# Troubleshooting

## Services Not Running

Browser, JSON DB, and RAG tasks require services. `tolokaforge run`
auto-starts them, but a cold first run can be slow while images build.
Pre-build once:

```bash
uv run tolokaforge docker build
```

Inspect running containers + their host-mapped ports with:

```bash
uv run tolokaforge docker status
```

Health of a running service is reachable at `/health` on the
host-mapped port `docker status` reports (e.g.
`http://localhost:<mapped>/health`). The container-internal
convention is: `db-service` on 8000, `mock-web` on 8080,
`rag-service` on 8001.

## Every Trial Fails at Registration

Engine and runner image must come from the same release, and a skew in **either**
direction fails every trial at registration. Both are fixed the same way — rebuild the
image from the tree you are running, or pin an image tag built from a matching engine:

```bash
make docker-build-core
```

**The image is newer than the engine.** The error names an engine protocol version.
`ENGINE_PROTOCOL_VERSION`
([`tolokaforge/runner/protocol.py`](../tolokaforge/runner/protocol.py)) is declared on
every registration and the runner refuses anything below its own, so no trial starts
and no tokens are spent.

**The engine is newer than the image.** The error is a Pydantic validation failure —
`extra_forbidden` — naming a field the older image's config models do not declare. The
engine emits several grading keys whether or not the pack declares them, so any pack at
all reproduces this against an image older than the engine. A field's declared *value*
shape locks the same way: the error is then a `string_type` or a value error naming the
key rather than an `extra_forbidden`.
The trial spec crosses the wire as a JSON string parsed by `extra="forbid"`
models, so an unknown key there is an error rather than a dropped field — unlike a
proto message field, which an older runner ignores.

Which keys bite, from which release, and in which direction is one table:
[GRADING.md](GRADING.md#runner-engine-version-lock) § Runner-engine version lock. For
the protocol-version half of the pairing see
[RUNNER.md](RUNNER.md#engine--image-version-lock) § Engine / image version lock.

## Every Tool Call Fails: MCP server closed connection

**Symptom.** Every tool call of every task declaring an `mcp_server.py` comes back
`MCP server closed connection`. The agent burns its whole turn
budget on failing tools and the trial grades `0.0` — a full-price run that measured nothing.
Reproduced on `examples/native/native_shared_domain`: three trials, `tool_calls=5`, every
call failed, `avg_score_micro=0.0`.

**Cause: the image resolved its own `mcp` version.** The runner image installs the built
wheel with **pip**, which resolves the declared dependency range itself rather than reading
`uv.lock` — so a loose range picks up a version inside the container that the workspace venv
never sees. `core/tools_interface.py` imports `mcp.server.fastmcp`, which `mcp` 2.x does not
have (FastMCP moved), and the server subprocess dies at import. The host venv stays green
throughout, which is what makes this hard to see: `uv run pytest` cannot reproduce it.

**Fix.** Rebuild the image from the current tree, which now resolves the pinned range:

```bash
make docker-build-core
```

**Anyone holding an image built from an unpinned resolution must rebuild it**, whatever the
tree they build from says today — the version is baked into the image. See #794.

## Browser Tool Errors

- Ensure Playwright is installed:
  ```bash
  uv run playwright install --with-deps chromium
  ```
- For Docker runtime, make sure the `runner` container is healthy —
  `tolokaforge docker status` shows per-container state.

## RAG Search Returns Empty

- Confirm the corpus directory exists in the task.
- Indexing is per trial and the runner drives it: when a task declares a rag
  corpus, the runner reads the corpus at trial registration and posts it to
  `/trials/{trial_id}/index`. There is no operator step to trigger — an empty
  result means the corpus never reached the service, so read the runner's log
  for that trial.
- Check the service itself with `GET /health` on the mapped port. `503
  degraded` means its embedding model failed to load, and the `reason` field
  names the model and the failure. Such a service still answers searches, but
  with BM25 keyword matching only — a query that needs semantic similarity
  comes back empty or off-target until the model loads. See
  [`REFERENCE.md`](REFERENCE.md) § RAG Service API for the routes.

## API Keys Not Found

Set a provider key in `.env` or your shell environment:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

## Task Validation Fails

```bash
uv run tolokaforge validate --tasks "tasks/**/task.yaml"
```

Common causes:
- Invalid YAML
- Missing required fields
- Tool name not in built-in list

## A Task Did Not Appear In The Run

**Symptom.** `tolokaforge run` finishes without running trials for a task
that discovery lists; the summary shows a shorter task set than the
config selected. The orchestrator log carries a single
`Failed to load task task_id=... error=...` line at error level.

**Cause.** By default the adapter's `get_task()` failure for one task id
is logged and the loader moves on to the next id — a design that keeps
partial-set runs going when one pack in a large glob is broken.

**Fix.** Turn the log line into a startup failure with the task id:

```yaml
orchestrator:
  strict_task_load: true
```

The exception propagates naming the offending task, so the run refuses
to start rather than proceeding with a silently shorter task list.
`--dry-run` already surfaces the same error regardless of the flag
(that path has no exception handling), so a dry run is the fastest way
to inspect the failure before you commit to the flag. See
[CONFIG.md § orchestrator.strict_task_load](CONFIG.md#run-configuration-runyaml).
