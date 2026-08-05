# Tools Reference

Tolokaforge exposes built-in tools via function calling. Enable them per task in `task.yaml`.

## Built-in Tools

- `browser`: Playwright-based browser automation (coordinate actions).
- `mobile`: Mobile app interaction tool (tap/type/scroll/app switching).
- `bash`: Allowlisted shell execution.
- `bash_session`: Persistent bash shell; cwd, environment, and functions persist across calls.
- `str_replace_editor`: View, create, and edit files with `view`/`create`/`str_replace`/`insert`.
- `read_file`: Read from `/env/fs/agent-visible`.
- `write_file`: Write to `/env/fs/agent-visible`.
- `list_dir`: List files in `/env/fs/agent-visible`.
- `db_query`: JSONPath query against JSON DB service.
- `db_update`: JSONPath updates against JSON DB service.
- `sql_query`: SQL query against JSON DB service.
- `get_db_schema`: SQL schema inspection for JSON DB tables.
- `search_kb`: RAG search over a per-trial corpus index. Functional for native
  tasks — declare `initial_state.rag.corpus_dir` and the runner indexes that
  corpus into the rag-service per trial (see `docs/TASKS.md`).
- `http_request`: Restricted HTTP client for mock web services.
- `build_check`: Zero-argument peer-service HTTP probe (compile / interface
  check). See [`build_check`](#build_check) below.
- `calculator`: Safe arithmetic calculator.

## Browser and Mobile Action Reference

`browser` supports these action types:

- `open_web_browser`, `navigate`, `wait_5_seconds`, `go_back`, `go_forward`, `search`
- `click_at`, `select`, `hover_at`, `type_text_at`, `key_combination`
- `scroll_document`, `scroll_at`, `drag_and_drop`

`mobile` supports these action types:

- `open_app`, `click_at`, `type_text_at`, `scroll_document`, `scroll_at`
- `key_combination`, `wait_5_seconds`, `go_back`, `drag_and_drop`, `select`, `press_enter`

See [BROWSER_TOOLS.md](BROWSER_TOOLS.md) for action schemas, examples, and coordinate behavior.

## Persistent Shell and Editor Tools

`bash_session` and `str_replace_editor` mirror Anthropic's agent tool contracts.
Each has two provider variants — a local variant and a docker-compose variant —
selected purely by `tool_config`: pass a `service` key to route into a container,
omit it to run locally. The wire schema the LLM sees is identical for both
variants. Both tools are wrapped by the runner, which drives their per-trial
lifecycle (see [RUNNER.md § Tool Lifecycle](RUNNER.md#tool-lifecycle)) and the
design rationale in [ADR 0017](adr/0017-persistent-agent-shell-and-editor-tools.md).

### `bash_session`

Session-lifetime shell matching Anthropic's `bash_20250124` contract.

**Input schema**

- `command` (string): the bash command to run.
- `restart` (boolean): reset the shell to a clean state; omit `command` when
  restarting (`{"restart": true}`).

Both fields are optional at the schema level; a call must supply one or the
other, and a call with neither fails loud.

**Session semantics.** The working directory, exported environment variables,
shell functions, and aliases persist across `execute()` calls for the life of
the trial. `restart` discards that state and yields a clean shell.

**Timeout.** Per-command timeout defaults to 120 s, configurable via
`tool_config.timeout_s`. On timeout the command is terminated and a
`[timed out after <n>s; command terminated]` note is appended to the output.

**Output.** Middle-truncated at 16384 characters (an elision marker names the
elided character count and hints to re-run a narrower command or `grep` for the
pattern). Non-zero
exit codes are surfaced as an `[exit code: <n>]` suffix.

**Provider variants**

- **Local** (no `service` key): a `bash` subprocess held under a PTY with job
  control (`set -m`). A timed-out command runs in its own foreground process
  group; terminating that group (SIGINT then SIGKILL) leaves the session-leader
  shell alive, so session state survives a per-command timeout.
- **Compose** (`service` + `compose_project_prefix`): a held
  `docker exec` into an already-running service container. It never brings the
  compose stack up or down — the environment / another lifecycle consumer owns
  that. Kill-safety is weaker than the local variant: signalling the host-side
  `docker exec` process group does not reach the command running inside the
  container, so on a per-command timeout the host exec client is killed and the
  in-container session is restarted. Session state accumulated before a
  timed-out command is lost (unlike the local variant, which preserves it), and
  the runaway command may briefly survive as an in-container orphan.

The compose variant resolves its target container as
`<compose_project_prefix><trial_id>_<service>`, with any `:` in the trial id
replaced by `_`; both compose tools share this resolution so they target the
identical container.

The runner reaches that sibling container's daemon over the host docker socket.
Materialisation bind-mounts `/var/run/docker.sock` into the runner service
automatically whenever a task routes a shipped tool through the compose variant
— the same trigger that bakes the docker CLI into the runner image — so the
task-declared compose file does not need to (and should not) supply it.

> **Compose runtime seam.** A default `docker compose up` names containers
> `<project>-<service>-<N>` (hyphens plus an ordinal index) — that scheme does
> not match the wrapper's `<compose_project_prefix><trial_id>_<service>`
> resolution, so a generic per-trial runtime brings up containers the compose
> tools cannot reach. Until this reconciles, a task pack enabling the compose
> variant must pin `container_name:` on the target service to exactly what the
> wrapper resolves, or the first `docker exec` fails with a no-such-container
> error. Tracked as
> [#585](https://github.com/Toloka/tolokaforge/issues/585). The local variants
> are unaffected.

**When to use.** Prefer `bash_session` for multi-step workflows that need a
persistent cwd, environment, or shell functions across turns. Use the legacy
per-call [`bash`](#built-in-tools) for one-shot allowlisted commands.

### `str_replace_editor`

File editor matching Anthropic's `str_replace_based_edit_tool` contract (the
`text_editor_20250429` / `text_editor_20250728` shape — parameter-identical;
`text_editor_20250728` adds a `max_characters` view-truncation knob). `undo_edit`
is absent, as in those Claude-4 variants.

**Input schema.** `command` (enum: `view`/`create`/`str_replace`/`insert`) and
`path` are required; the remaining fields apply per command: `view_range`,
`file_text`, `old_str`, `new_str`, `insert_line`, `insert_text`.

**Commands**

- `view` — `path` plus optional `view_range: [start, end]` (1-indexed; `-1` for
  end of file). Renders `cat -n`-style line-numbered content for a file, or a
  directory listing 2 levels deep (hidden entries and `__pycache__` skipped).
  An out-of-range `view_range` fails loud.
- `create` — `path` + `file_text`. **Fails loud if the path already exists**
  (a deliberate deviation from Anthropic's reference, which overwrites).
- `str_replace` — `path` + `old_str` + `new_str`. Replaces the single unique
  occurrence of `old_str`; fails loud on zero matches or more than one (the
  error reports the match count).
- `insert` — `path` + `insert_line` + **`insert_text`** (note: `insert` uses
  `insert_text`, not `str_replace`'s `new_str`). `insert_line: 0` inserts before
  the first line; a positive `N` inserts after line `N`.

**Fail-loud framing.** Ambiguous, destructive, or out-of-range operations raise,
and the error reaches the LLM for self-correction. Mutating commands
(`create`/`str_replace`/`insert`) require valid UTF-8 and fail loud on a
non-UTF-8 file (which cannot be safely round-tripped); `view` reads with
replacement characters for display only.

**Path validation.** Paths resolve to their realpath and must stay contained in
the working root; a symlink escape or a `..` component that leaves the root
fails loud. The root defaults to `/work` and is set per task via
`tool_config.working_root` (see [Enabling Tools](#enabling-tools)); all four
commands and the containment check bind to that effective root. A configured
root that does not exist or is not a directory fails loud on first use, naming
the root — the tool never silently creates it.

**Provider variants**

- **Local** (no `service` key): in-process file operations rooted at the
  effective working root (`working_root`, default `/work`) on the runner
  container. Writes go to a temp file and are renamed into place (single-process
  temp+rename).
- **Compose** (`service` + `compose_project_prefix`): every command runs through
  `docker exec` into an already-running service container, rooted at the same
  effective working root. File content is piped on stdin (never interpolated
  into the shell command string) and paths are passed as positional arguments,
  so agent-controlled bytes cannot inject shell commands. Path containment is
  validated **inside the container** via `realpath`; if `realpath` is absent
  from the target image the engine fails loud rather than silently skipping
  validation. Write atomicity is weaker than the local variant: a completed
  temp-file+`mv` is atomic, but an exec interrupted mid-write can leave the temp
  file behind. The editor has no configurable per-command timeout.

### `build_check`

Zero-argument HTTP probe against a compose peer service on the trial's
private network. Named + shaped for the compile / interface-collection
checks that code-migration and code-generation benchmarks perform
against a hidden test harness before invoking the full graded suite —
see [ADR 0029](adr/0029-build-check-builtin-tool.md).

**Input schema.** None. The tool advertises zero parameters and ignores
any inbound kwargs. The endpoint is fully declared at task-authoring
time via `tool_config`; the agent cannot redirect the probe.

**`tool_config` fields**

- `service` (string, required) — compose service name to probe.
- `port` (int, default `8001`) — port on that service.
- `path` (string, default `"/build_check"`) — endpoint path.
- `method` (`"GET"` | `"POST"`, default `"POST"`) — HTTP verb. POST
  sends an empty JSON body (`{}`).
- `timeout_s` (float, default `300.0`) — request timeout.

**Response contract.** The tool returns the peer service's response
body verbatim as tool output — the peer owns the payload shape. On a
non-2xx status the body is still returned but the result is marked as
an error so the loop records `EXECUTION_STATUS_ERROR`; on timeout or
connect failure the tool returns a structured error message.

**Network scope.** The request goes to a docker-DNS-resolved compose
peer on the trial's private network. No external egress; honours
`NetworkPolicy.NO_INTERNET` by construction.

**Enabling.** List `build_check` in `enabled` and declare the endpoint
under `tools.agent.build_check`:

```yaml
tools:
  agent:
    enabled: ["build_check", "bash_session", "str_replace_editor"]
    build_check:
      service: grader        # compose service name in the task's compose file
      port: 8001
      path: /build_check
```

## Enabling Tools

```yaml
tools:
  agent:
    enabled: ["browser", "db_query", "db_update", "search_kb"]
  user:
    enabled: []
```

### Persistent shell and editor

List the tool in `enabled` to get its local variant — no kwargs block is needed:

```yaml
tools:
  agent:
    enabled: ["bash_session", "str_replace_editor"]
```

To override the default working root (`/work`), add `working_root` under
`tools.agent.str_replace_editor`. This applies to both the local and the compose
variant; a missing or non-directory root fails loud on first use.

To select the compose variant, add a per-tool kwargs block under
`tools.agent.<name>` naming the target `service` and the `compose_project_prefix`
used to bring the stack up. `bash_session` additionally accepts `timeout_s`;
`str_replace_editor` additionally accepts `working_root` (see above). Both
compose variants accept an optional `user` field (a `--user` value passed to
`docker exec` — a name or `uid:gid`) so an agent-facing session can drop
privileges when the target container's ENTRYPOINT itself runs as root. Default
`None` inherits the container's default user. The editor has no configurable
per-command timeout:

```yaml
tools:
  agent:
    enabled: ["bash_session", "str_replace_editor"]
    bash_session:
      service: main
      compose_project_prefix: env_
      timeout_s: 120
      user: model             # optional; docker exec --user; default inherits
    str_replace_editor:
      service: main
      compose_project_prefix: env_
      working_root: /srv/agent  # optional; defaults to /work
      user: model             # optional; docker exec --user; default inherits
```

> **Compose-variant network isolation.** Under `network_policy: no_internet` /
> `limited_internet`, the engine attaches every compose service to a single
> injected internal network. If a task pack co-locates env services (e.g. a DB)
> with the agent-side compose service that `bash_session` /
> `str_replace_editor` executes into, the agent can reach those services
> directly and bypass wrapped tools. Tracked as
> [#581](https://github.com/Toloka/tolokaforge/issues/581). Until that lands, do
> not co-locate env services with the `bash_session` / `str_replace_editor`
> target service. The local variants are unaffected.

## MCP Tools

Custom tools can be provided via an MCP server:

```yaml
tools:
  agent:
    enabled: ["custom_tool"]
    mcp_server: "../mcp_server.py"
```

See `docs/MCP_INTEGRATION.md` for details.
