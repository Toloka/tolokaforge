# Security Model

Tolokaforge evaluates untrusted LLM agents. This document describes the security boundaries that exist today and the threat model they address.

## Architecture Overview

Tolokaforge uses Docker-based isolation for all tool execution. The orchestrator runs on the host (or in its own container) and proxies tool execution to a containerised executor via gRPC. Environment services run on an internal Docker network.

```
┌─────────────────┐
│  Orchestrator   │ ← LLM API access (default network)
└────────┬────────┘
         │ gRPC (env-net)
┌────────▼────────┐
│    Executor     │ ← cap_drop: ALL, no-new-privileges
└────────┬────────┘
         │ HTTP (env-net, internal: true)
    ┌────▼─────┬──────────┬──────────┐
    │ JSON DB  │ Mock Web │   RAG    │
    └──────────┴──────────┴──────────┘
```

This architecture provides:

- **Executor** runs with `cap_drop: ALL`, `cap_add: NET_BIND_SERVICE`, `security_opt: no-new-privileges`. It can only reach services on `env-net`.
- **env-net** is an internal bridge network (`internal: true`) with no external internet access.
- **Orchestrator** is the only component with external network access (for LLM API calls).
- **Environment services** (JSON DB, Mock Web, RAG) are on `env-net` and also exposed to localhost for development convenience.

See `tolokaforge/docker/stacks/` for the predefined service stack definitions.

## Tool-Level Security

### Tool Allowlisting

Tasks declare exactly which tools the agent and user can call:

```yaml
tools:
  agent:
    enabled: ["db_query", "browser", "read_file"]
  user:
    enabled: ["user_check_device"]
```

The `ToolExecutor` enforces:

1. Only registered tools can be called
2. Arguments are validated against JSON schemas (`additionalProperties: false`)
3. Missing required parameters are rejected
4. Per-tool rate limits are enforced (configurable via `ToolPolicy.rate_limit`)
5. Per-tool timeouts are enforced (configurable via `ToolPolicy.timeout_s`, default 30s)

### Rate Limits and Timeouts

Multiple timeout layers prevent runaway execution:

| Control | Default | Config path |
| --- | --- | --- |
| Per-tool timeout | 30s | `ToolPolicy.timeout_s` |
| Per-turn timeout | 60s | `orchestrator.timeouts.turn_s` |
| Episode timeout | 1200s | `orchestrator.timeouts.episode_s` |
| Max turns | 50 | `orchestrator.max_turns` |
| Request throttle | 1.0/s | `orchestrator.max_requests_per_second` |

### Budget Cap

`orchestrator.max_budget_usd` sets a hard spend limit. The orchestrator tracks cumulative estimated cost and stops leasing new work when the cap is reached.

### Log Redaction

Tool call arguments are logged with automatic redaction of keys containing `password`, `token`, `secret`, or `api_key`. See `ToolExecutor._redact_sensitive()` in `tolokaforge/tools/registry.py`.

## Secret Management

- **`SecretManager`** (`tolokaforge/secrets`) is the canonical way to access secrets in code. It reads `.env` via `DotEnvProvider` without polluting `os.environ`. CLI entry (`tolokaforge run`) calls `init_default()` automatically. Do not use `os.environ.get()` or `load_dotenv()` for API keys.
- **Container secret injection**: Secrets are injected into containers via `SecretManager.to_env_dict()`. Only the specific keys needed (derived from grading config `llm_judge.model_ref`) are passed — not all available secrets. This is controlled by `ServiceDefinition.secret_keys`.
- **Grading assets** (`grading.yaml`, expected states) are read by the orchestrator after trial completion. They are never passed to the agent or exposed through tool outputs.
- **`.env`** is gitignored and never mounted into executor or environment containers.

## Ground Truth Isolation

The agent never sees grading criteria or expected outputs:

- `grading.yaml` and expected state files stay on the host
- Environment services reset per trial from fixtures
- Grading runs post-trial using host-side data only
- JSON DB state is namespaced per `{task_id}_{trial_idx}` so trials cannot interfere with each other

## Threat Model

### Addressed

| Threat | Mitigation |
| --- | --- |
| Agent calling unauthorized tools | Tool allowlisting + schema validation |
| Runaway execution (cost/time) | Budget cap, episode timeout, max turns, per-tool timeout |
| Agent accessing grading criteria | Grading data is host-side only, never in tool outputs |
| Environment state leaking between trials | Per-trial namespace isolation in JSON DB |
| Executor reaching external internet (Docker mode) | `env-net` is `internal: true` |
| Executor privilege escalation (Docker mode) | `cap_drop: ALL`, `no-new-privileges` |
| Sensitive data in logs | Automatic key redaction in tool call logging |

### Not Addressed

| Threat | Notes |
| --- | --- |
| Host-level Docker escapes | Out of scope; assumes Docker daemon is trusted |
| Supply chain attacks in task code | Task authors are assumed trusted |
| Side-channel attacks | Not mitigated |
| Agent exfiltrating data via LLM output | The orchestrator relays model output; no content filtering |


## Testing

Security-related tests live in `tests/integration/test_security.py`:

- `TestToolAllowlisting` — unregistered tool rejection, schema validation, rate limiting
- `TestDockerSecurity` — verifies `env-net` is `internal: true` in docker-compose.yaml
- `TestNetworkIsolation` — executor connectivity checks (requires Docker)

Run them:

```bash
# Tool-level tests (no Docker needed)
uv run pytest tests/integration/test_security.py -v -k "Allowlisting"

# Docker isolation tests (requires running containers)
docker compose up -d
uv run pytest tests/integration/test_security.py -v -m requires_docker
```

## Security Checklist

Before running evaluations:

- [ ] API keys in `.env`, not committed to git
- [ ] `orchestrator.max_budget_usd` set for long runs
- [ ] Task YAML uses minimal tool allowlist (don't enable `bash` unless needed)
- [ ] Ensure `runtime: "docker"` is set and Docker services are running (`docker compose up -d`)
- [ ] Verify `env-net` is `internal: true` in `docker-compose.yaml`
