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
`extra_forbidden` — naming a field the older image's config models do not declare.
Three fields carry it: `state_checks.hash_weight`, which appears for **every** pack
carrying a non-empty `state_checks:` block, `transcript_rules.min_assistant_turns`,
which appears for **every** pack carrying a `transcript_rules:` block, and
`trace_checks`, which appears for **every** pack. The engine emits all three whether
or not the pack declares them, so any pack at all reproduces this against an image
older than the engine.
A field's declared value shape locks the same way: a pack declaring a composite
(list-valued) `state_checks.id_fields` key against an image that predates the list
form is rejected with a `string_type` error naming `id_fields`, and a
`combine_method` value the image's closed set does not hold is rejected at the
value.
The trial spec crosses the wire as a JSON string parsed by `extra="forbid"`
models, so an unknown key there is an error rather than a dropped field — unlike a
proto message field, which an older runner ignores.

See [RUNNER.md](RUNNER.md#engine--image-version-lock) § Engine / image version lock
and [GRADING.md](GRADING.md#hash-based-grading-tau-bench-compatible) §
"Runner-engine version lock (both directions)".

## Every Tool Call Fails: MCP server closed connection

**Symptom.** Every tool call of every task declaring an `mcp_server.py` comes back
`Tool error: RuntimeError: MCP server closed connection`. The agent burns its whole turn
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
- Trigger indexing:
  ```bash
  curl -X POST http://localhost:8001/index \
    -H "Content-Type: application/json" \
    -d '{"corpus_path": "/app/tasks/<category>/<task>/rag/corpus"}'
  ```

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
