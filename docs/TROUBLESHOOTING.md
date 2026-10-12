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
key rather than an `extra_forbidden`. A model-config field reads differently again: an
image of this engine version or newer refuses it as `Value error, ModelConfig was given a
key it does not declare` (or `OpenRouterConfig`, `ModelSessionConfig`, `ReasoningConfig`
for a nested block) at `agent_model_config`, `user_model_config` or `judge_model_config`,
naming the key and every key the image accepts; an older image drops it without a word.
A spec the older image does parse is refused by the engine instead, with `runner image
declares wire-protocol version N; engine requires M`: the engine reads the image's
version off every successful registration and refuses one below its own.
Same cause, same fix.
The trial spec crosses the wire as a JSON string parsed by `extra="forbid"`
models, so an unknown grading key there is an error rather than a dropped field — unlike a
proto message field, which an older runner ignores.

Which keys bite, from which release, and in which direction is one table:
[GRADING.md](GRADING.md#runner-engine-version-lock) § Runner-engine version lock. For
the protocol-version half of the pairing see
[RUNNER.md](RUNNER.md#engine--image-version-lock) § Engine / image version lock.

## A Large Task World Fails to Register or Capture

**Symptom.** Trials of a task with a large initial state fail at registration,
or their final-state capture fails, with an error such as:

```
RegisterTrial failed: the message exceeds the gRPC message size limit (CLIENT: Sent message larger than max (73400320 vs. 52428800)). The engine's limit is 50 MiB, set by `compute.grpc_max_message_mb`; ...
GetState failed: the message exceeds the gRPC message size limit (CLIENT: Received message larger than max (73400320 vs. 52428800)). ...
```

A grader reading the state through `SubstrateService` reports the same gRPC
`RESOURCE_EXHAUSTED` status inside `substrate unreachable at ...`.

**Cause.** Every gRPC message between the engine, the runner and the grader is
bounded by `compute.grpc_max_message_mb` (default 50 MiB), and the trial's
world does not fit. The two figures in gRPC's detail are the message size and
the bound that refused it, in bytes; `CLIENT:` means the engine's own bound
refused it, `SERVER:` means the runner's (its bound differs from the engine's,
for example because a task compose file pins its own value).

**Fix.** Raise the bound in the run config, above the world's serialized size
(at most 2047 MiB):

```yaml
compute:
  grpc_max_message_mb: 200
```

The engine passes the value to every runner container it starts. A task
compose file that sets `TOLOKAFORGE_GRPC_MAX_MESSAGE_MB` on its runner keeps
that value, so raise it there too; a separately deployed runner or grader
needs the same variable. Memory per trial grows with the message on both
sides — see [RUNNER.md § gRPC message size](RUNNER.md#grpc-message-size).

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

## litellm_models entry '…' does not apply to provider '…'

**Symptom.** `config validate` reports an ERROR at `models.<role>.name`, or
`run` / `prepare` / `worker` stop before any trial, with:

```text
litellm_models entry 'self-hosted/qwen3.6-35b-a3b' does not apply to provider 'openai', name 'self-hosted/qwen3.6-35b-a3b': that config is looked up under 'openai/self-hosted/qwen3.6-35b-a3b', so the entry admits nothing. Rename the entry to 'openai/self-hosted/qwen3.6-35b-a3b'.
```

**Cause.** A `litellm_models:` entry in the presets overlay is keyed on the
config's `name` alone. The key is `<provider>/<name>`, the model string the
client sends, with the `name` verbatim, so an entry under the bare name is
never found and the provider still refuses `tools` before sending.

**Fix.** Rename the key in the overlay to the one the message names. See
[`LLM_LAYER.md`](LLM_LAYER.md#when-litellm-has-never-heard-of-the-model).

## A route-prefixed model name resolves to the 'default' preset

**Symptom.** `config validate` reports a WARNING at `models.<role>.name`:

```text
⚠ [WARNING] models.agent.name: 'self-hosted/nova-pro-v1' (provider 'openai') resolves to the 'default' preset, but its last segment 'nova-pro-v1' matches preset 'aws_nova' (hint: If preset 'aws_nova' fits the model this route serves, add an overlay preset whose match covers the full name (match: ['*/nova-pro-v1'] or match: ['self-hosted/nova-pro-v1']) carrying the policies of preset 'aws_nova'; or, when the route also serves the unprefixed name, name the model 'nova-pro-v1'.)
```

The run logs one WARNING per such model config after its tasks load, with the
same facts and remedy as fields:

```text
WARNING | family=aws_nova last_segment=nova-pro-v1 model_name=self-hosted/nova-pro-v1 path=models.agent.name provider=openai remedy="If preset 'aws_nova' fits …" | A route-prefixed model name resolves to the 'default' preset, but its last segment matches another preset
```

**Cause.** No preset matches the full name, but the name's last segment alone
matches a preset. The bundled presets claim their models under any route
prefix, so this points at a preset whose globs do not: typically an overlay
preset with a bare glob and no `*/`-prefixed sibling, or `aws_nova`, which
routes by provider. The run proceeds on `default`, whose policies may not fit
the model.

**Fix.** If the preset fits the model this route serves, add an overlay preset
whose `match` covers the full name, as the hint shows. When the route also
serves the unprefixed name, naming the model that way works too. See
[`LLM_LAYER.md`](LLM_LAYER.md#preset-coverage).

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
