# Security Model

Tolokaforge evaluates untrusted LLM agents. This document describes the security boundaries that exist today and the threat model they address.

## Architecture Overview

Tolokaforge runs all tool execution inside Docker containers. The orchestrator runs on the host and proxies tool execution to a containerised **runner** over gRPC. There are two ways the runner and its supporting services are materialised, and they carry different network postures.

### Built-in stack (Case A — the default)

When a run declares no `environment_manifest`, the orchestrator brings up the engine's built-in services with `EngineStack` (`tolokaforge/docker/stack.py`): `core_stack` = runner + db-service, or `full_stack` = runner + db-service + mock-web + rag-service. Every built-in service is attached to `runner-net`, a bridge network that is **not** `internal`.

- The **runner** runs with `cap_drop: ALL`, `cap_add: NET_BIND_SERVICE`, and `no-new-privileges` (its default `ResourcePolicy`; relaxed only on the Docker-socket / Docker-in-Docker terminal-bench paths).
- `runner-net` has **full egress by construction**. The runner needs it: when a task declares an `llm_judge` grading component, `RunnerService.GradeTrial` runs the rubric judge *inside* the runner container and must reach the LLM provider to grade. The runner's published gRPC port must also stay host-reachable so the orchestrator can connect to it.
- This is **not** a threat vector on this path. `EngineStack` materialises only first-party engine services — there is no untrusted task-declared compose to isolate. `network_policy` is a task-declared-stack concept; it lives on `EnvironmentManifest`, which a Case A run does not have. Egress of tools the agent runs *inside* the runner is a separate concern (tracked in #325); a container-network posture cannot address it.

See `tolokaforge/docker/stacks/` for the predefined built-in stack definitions.

### Task-declared stack (Case B / Case C)

When a task declares its own `environment_manifest.compose_file`, the runtime backend materialises that task-declared compose stack instead — arbitrary services the task author chose. This is the path with an enforceable network posture: `EnvironmentManifest.network_policy` (default `no_internet`) is applied by `enforce_network_policy` (`tolokaforge/core/compose_materialisation.py`) as an in-place rewrite of the copied compose file before it is brought up.

- Under `no_internet` every task service (unless opted out via `network_access: restricted`) joins an injected `internal: true` network (`tolokaforge_netpolicy_internal`) — no application service can reach the public internet, while inter-service DNS stays intact. The runner is *additionally* attached to a non-internal edge network (`tolokaforge_netpolicy_edge`) so it keeps its control-plane and grading egress. The honest scope: **task-declared application services get zero public egress; the runner retains its control-plane and grading egress.**
- `full_internet` runs the compose file unchanged. `limited_internet` attaches application services to the internal network with no direct egress and points them at an injected digest-pinned `ubuntu/squid` forward-proxy sidecar that default-denies egress and forwards only to the hosts in `limited_internet_allowlist` (CONNECT-only, no TLS interception; non-allowlisted egress is refused with HTTP 403). The runner keeps direct edge egress for grading, as under `no_internet`.
- A per-service opt-out, `services.<name>.network_access: restricted`, excludes the named service from the injected shared internal network — the service joins only the networks its compose entry declares. Under `limited_internet` a restricted service also receives no `HTTP(S)_PROXY` env (there is no path from it to the proxy sidecar). Task-declared networks the restricted service joins are still forced `internal: true`, so it remains egress-blocked at the docker-network level; the runner posture is unchanged. The manifest validator refuses `runner_service` marked `restricted` and refuses a restricted service whose compose entry declares no networks.

See [ADR-0018](adr/0018-multi-container-under-shared-runtime.md) for the full case matrix, [RUNTIME_BACKENDS.md](RUNTIME_BACKENDS.md) for backend mechanics, and [PROJECTS.md](PROJECTS.md) for the `network_policy` and per-service isolation authoring surface.

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
| Max turns | 50 | `orchestrator.max_turns` (always-on cap; #534 will flip to opt-in) |
| Request throttle | 1.0/s | `orchestrator.max_requests_per_second` |

### Budget Cap

`orchestrator.max_budget_usd` sets a hard spend limit. The orchestrator tracks cumulative estimated cost and stops leasing new work when the cap is reached.

### Tool Call Arguments

Tool call arguments are recorded verbatim on `RecordedToolCall.arguments`, on both
grading substrates. The recorded tool calls are the grader's input: transcript
rules match declared actions against recorded arguments, so rewriting an argument
value there changes a grading verdict. A trial whose agent passed a
`token`-named argument would fail an argument match it should have passed —
and would fail it on one substrate only, since the runner has never rewritten
anything.

Redaction belongs at artifact-write time, where the concern actually is —
discretion protects a written artifact, and must not corrupt what the grader
reads. That layering is tracked in #694; until it lands, treat a recorded run's
tool-call arguments as carrying whatever the agent passed.

## Secret Management

- **API keys** are read only through `SecretManager` (never `os.environ` directly). The orchestrator holds them on the host; the runner receives them serialised into the single `TOLOKAFORGE_SECRETS_JSON` environment variable and reconstructs its own `SecretManager` singleton, because it runs in-container LLM-as-judge grading. This is the only place credentials cross the host→container boundary — never via build args, mounts, or image bake-in. Environment services (db-service, mock-web, rag-service) receive no secrets.
- **Grading assets** (`grading.yaml`, expected states) are read by the orchestrator after trial completion. They are never passed to the agent or exposed through tool outputs.
- **`.env`** is gitignored and never mounted into the runner or environment containers.

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
| Environment state leaking between trials | Per-trial namespace isolation in JSON DB (built-in stack); per-trial containers, networks, and volumes under `PerTrialRuntimeBackend` (task-declared stack) |
| Task-declared services reaching external internet | `network_policy: no_internet` (the default) attaches them to an injected `internal: true` network via `enforce_network_policy` (Case B/C) |
| Untrusted sibling service reaching first-party sibling services (db-service, runner) via the shared injected network | `network_access: restricted` on the untrusted service excludes it from the injected shared internal network; the compose file's task-declared networks scope which siblings it can reach. |
| Runner privilege escalation (Docker mode) | `cap_drop: ALL`, `cap_add: NET_BIND_SERVICE`, `no-new-privileges` |
| Sensitive data in logs | Automatic key redaction in tool call logging |

### Not Addressed

| Threat | Notes |
| --- | --- |
| Built-in runner reaching external internet (Case A) | Accepted risk. `EngineStack` materialises only first-party engine services; the runner needs LLM-provider egress for in-container LLM-as-judge grading, so `runner-net` is `full_internet` by construction and a Case A run carries no `network_policy` surface. |
| Egress of tools the agent runs inside the runner | Not blocked by `no_internet` — such tools share the runner's edge access. Tracked in #325. |
| Host-level Docker escapes | Out of scope; assumes Docker daemon is trusted |
| Supply chain attacks in task code | Task authors are assumed trusted |
| Side-channel attacks | Not mitigated |
| Agent exfiltrating data via LLM output | The orchestrator relays model output; no content filtering |


## Testing

- **Tool allowlisting** — `tests/unit/test_tool_security.py::TestToolAllowlisting`: unregistered tool rejection, schema validation, rate limiting. No Docker needed.
- **`Network.internal` primitive + built-in non-internal invariant** — `tests/unit/test_network_internal.py`: `Network.create(..., internal=…)` faithfully carries docker-py's `internal` flag, and `EngineStack.create_networks()` always creates its networks non-internal (the executable form of "Case A is `full_internet` by construction"). No Docker needed.
- **Task-declared `network_policy` enforcement (Case B/C)** — `tests/canonical/test_network_policy_enforcement.py` snapshots the `no_internet` compose-file topology produced by `enforce_network_policy`; `tests/canonical/test_environment_manifest_contract.py::TestNetworkPolicyContract` pins the wire shape and `no_internet` default. No Docker needed.
- **Runner network isolation (real daemon)** — `tests/integration/test_security.py::TestNetworkIsolation` moves a runner onto an internal network and asserts it reaches db-service but not the public internet. This demonstrates a runner *can* be network-isolated when its trial makes no in-container LLM-judge call; the built-in `runner-net` is deliberately non-internal so that grading egress is available.

```bash
# Unit + canonical security tests (no Docker needed)
uv run pytest tests/unit/test_tool_security.py tests/unit/test_network_internal.py -v
uv run pytest tests/canonical/test_network_policy_enforcement.py -v

# Runner network-isolation test (requires Docker)
uv run pytest tests/integration/test_security.py -v -m requires_docker
```

## Security Checklist

Before running evaluations:

- [ ] API keys in `.env`, not committed to git
- [ ] `orchestrator.max_budget_usd` set for long runs
- [ ] Task YAML uses minimal tool allowlist (don't enable `bash` unless needed)
- [ ] For task-declared stacks, leave `network_policy` at its `no_internet` default unless the task genuinely needs application-service egress (built-in-stack runs are `full_internet` by construction and have no `network_policy` surface)
