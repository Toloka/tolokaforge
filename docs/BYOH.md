# Bring your own harness (BYOH)

BYOH runs a supported agent CLI in a separate, per-trial Docker container while
Tolokaforge's Runner remains the source of truth for tools, state, history, and
grading. Set `agent_harness` at the top level of a run config; no `models.agent`
entry is required.

```yaml
models: {}

agent_harness:
  type: claude-code
  version: 2.1.203
  flags:
    model: claude-sonnet-4-6
    permission_mode: bypassPermissions
  env:
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
  user_simulator_policy: reject

orchestrator:
  workers: 2
  repeats: 2
  agent_network:
    mode: allowlist
    entries: [api.anthropic.com]
```

Credential-like keys are rejected in `agent_harness.env`. Put the adapter's
required key in a configured `SecretManager` provider. Claude Code requires
`ANTHROPIC_API_KEY`; Codex requires `OPENAI_API_KEY`. Subscription and OAuth
credentials are intentionally unsupported.

## Runtime and authentication matrix

| Adapter | Runtime | Authentication | MCP headers | ATIF | Resume |
|---|---|---|---:|---:|---:|
| Claude Code | Implemented; live key gate remains | `ANTHROPIC_API_KEY` | Yes | Yes | Capability declared; multi-turn integration deferred |
| Codex | Implemented; live key gate remains | `OPENAI_API_KEY` via `SecretManager` | Yes | Yes | Capability declared; multi-turn integration deferred |
| Generic ACP | Implemented and verified with a local mock | Agent-command specific | ACP HTTP server config | Yes | No |
| Cursor | Documentation only | Interactive | — | — | — |
| Copilot | Documentation only | Interactive | — | — | — |
| Gemini | Documentation only | Interactive | — | — | — |
| OpenCode | Documentation only | Interactive | — | — | — |

Codex's streamable-HTTP MCP configuration uses a per-trial authorization header,
and `codex exec --json` supplies the machine-readable event stream. See the
[Codex MCP configuration](https://developers.openai.com/codex/mcp) and
[non-interactive mode](https://developers.openai.com/codex/noninteractive).

## Single-shot user behavior

The current release passes only `initial_user_message` (falling back to the task
description). `user_simulator_policy: reject`, the default, rejects any task with
LLM or scripted follow-up behavior. `first_message_only` opts into dropping those
follow-ups; the loss is recorded in the trial task metadata. Resume-driven user
simulation is tracked separately.

## Isolation, MCP, and tool bypass

Each attempt gets a unique host workspace and mounts only that child at `/work`.
For auto-started runs, the same run-level workspace root is mounted into the
Runner at `/workspaces`, and registration selects only the matching attempt
child for provisioning, builtin file/bash tools, and file grading. Distributed
workers must mount their shared `<run-dir>/workspaces` directory into the Runner
at `/workspaces`; registration fails before agent startup when that mount is
missing or the requested path escapes it.
Agent containers receive no Docker socket and drop all Linux capabilities. The
Runner registers a random per-trial bearer token and exposes only that trial's
tool namespace over streamable HTTP. Missing, wrong, and cross-trial tokens are
rejected before dispatch. MCP and gRPC use the same execution function, so the
server-side ledger and grading state cannot diverge.

Do not enable a harness's unrestricted built-in network or host tools to work
around the gateway. They bypass the auditable Runner ledger and may invalidate
transcript rules. Claude runs with its built-in tools disabled; Codex remains
inside the outer Docker/resource/network boundary.

## Network policy

`orchestrator.agent_network.mode` is one of:

- `no-network`: internal Runner MCP access only.
- `public`: direct public networking; use only for trusted diagnostic runs.
- `allowlist`: default-deny internal network plus a dual-homed forward proxy.

When `agent_network` is omitted, Tolokaforge derives an allowlist from the adapter's
model-provider and installer hosts. `NO_PROXY` always covers `runner`, localhost,
and loopback. Denied destinations receive an explicit HTTP 403 and proxy decisions
are emitted as JSON audit records.

## Outputs and operations

Each `trials/<task>/<index>/harness/` directory contains:

- `stdout.log` and `stderr.log` with the raw CLI output;
- `atif.json` with imported ATIF v1.7 steps, tool observations, and metrics.

The normal trajectory, prompts, task config, grade, status, cost/token metrics,
and aggregate pass@k outputs are unchanged. Rate limits, overloads, transport
failures, and timeouts are retryable. Usage exhaustion and safety refusals are
terminal failures. `tolokaforge run --resume`, `status`, and `analyze` continue to
operate on the normal run directory.

## Vendoring and known limits

The adapted Harbor helpers are pinned to commit
`8083897a5df169d804c5afefd116c8fe6ffd9f8e`; see
`tolokaforge/harnesses/vendor/harbor/VENDORED.md` and `NOTICE` for source mapping
and the full patch log.

Live Claude and Codex acceptance runs require separately provided API keys. The
current implementation does not reuse subscription credentials, resume an agent
for user-simulator follow-ups, or claim support for interactive-auth adapters.
