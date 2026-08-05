# 0029. `build_check` as a generic peer-service HTTP probe in core

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Code-migration and code-generation benchmarks want a way for the agent to
run a *cheap compile / interface-collection check* against a hidden test
harness before invoking the full graded test suite. The reference-eval
shape for this — the one every code-migration benchmark converges on —
is a single-endpoint HTTP probe on a peer service that runs a fast build
command against the migrated repository, replies with `configured` +
`run_error` + `output_tail`, and takes no per-invocation arguments (the
build command is baked into the task).

The concrete first consumer lives in an adapter (the Migration Bench
adapter's `mb-grade` service exposes `/build_check`). But the tool
itself — an HTTP request to `http://{service}:{port}{path}` returning
the response body verbatim — has no adapter-specific logic. Any future
benchmark whose grader container exposes a similar endpoint should be
able to enable the same tool by name, with the endpoint declared in the
pack's `tool_config`. Putting the tool in an adapter would either force
every benchmark to re-implement the same HTTP wrapper or force other
adapters to depend on the first-consumer adapter — both bad. Wiring the
tool through the runner's existing `BuiltinGenericToolWrapper` seam
avoids both: the runner already resolves builtin tools by name against
the unified registry (ADR-0011 Pattern-A shape) and splats
`tool_config` into the tool's `__init__`.

## Decision Drivers

- **Adapter neutrality.** The tool must not name Migration Bench or any
  specific adapter. Every field of behaviour comes from `tool_config`;
  the tool's contract is "POST/GET this URL, return the body."
- **Config-driven endpoint.** Task pack declares
  `tool_config: {service, port, path, method, timeout_s}`. Same registry
  seam as `http_request` — the tool is instantiated once per trial with
  those kwargs.
- **No per-invocation arguments.** The schema advertises zero required
  and zero optional parameters. This matches the reference-eval shape
  (agent calls `tests_helper()` with no arguments) and prevents the
  agent from redirecting the probe to an arbitrary URL — the endpoint
  is authoritative from the task author.
- **Peer-service, not internet.** The request goes to a docker-DNS-
  resolved compose peer on the trial's private network. No external
  egress; honours `NetworkPolicy.NO_INTERNET` by construction.
- **Failures are tool errors, not infrastructure errors.** Non-2xx
  responses, timeouts, and network errors surface as `ToolResult(
  success=False, error=...)` so the loop records
  `EXECUTION_STATUS_ERROR` and hands the agent something to iterate on
  — the trial does not terminate.
- **Response body verbatim.** The peer service owns the payload shape.
  The tool does not re-interpret it, so a new adapter can ship a
  different response schema without touching core.

## Considered Options

1. **Ship `build_check` as a builtin in `tolokaforge.tools.builtin`
   registered on `Dispatch.GENERIC`.** Config-driven via `tool_config`.
   **This ADR.**
2. **Extend `http_request` with a "no-arg preset" mode.** Reuse the
   existing HTTP tool by declaring a canned URL in `tool_config` and
   locking the schema to zero arguments.
3. **Keep the tool in the Migration Bench adapter behind a public seam
   the adapter exports.** Ship a re-usable base class from
   `tolokaforge-adapter-common` (which does not exist today).
4. **Two tools: a core-level `peer_http_request` (agent-supplied path)
   and per-adapter thin wrappers.** Core tool takes a `path` argument
   from the agent; adapters register a wrapper that closes over the
   path.

## Decision

Option 1. `build_check` ships as a first-party builtin in
`tolokaforge/tools/builtin/build_check.py`, registered in
`tolokaforge.tools.builtin.registry._REGISTRY` under `Dispatch.GENERIC`.
The tool's `__init__` accepts `service` (required), `port` (default
8001), `path` (default `"/build_check"`), `method` (default `"POST"`),
and `timeout_s` (default 300 s). Its schema declares zero parameters.
Its `execute` ignores inbound kwargs and dispatches a single HTTP
request via `httpx`.

The Migration Bench adapter's grader service exposes the endpoint the
tool talks to; that is documented as the *first* consumer, not the
*only* consumer. New adapters that want the same shape enable the tool
by name in the task pack with their own `service` / `port` / `path`.

## Consequences

**Positive.**

- Any adapter with a peer service that follows the "one endpoint, no
  args, structured body" shape gets the tool for free.
- The tool's registration is the same shape as every other builtin —
  entry in `_REGISTRY`, entry in the canonical registry-lock test
  (`tests/unit/test_builtin_registry.py`), entry in the runner-subset
  partition (`tests/canonical/test_runner_subset_partition.py`).
- Zero-parameter schema means the agent cannot redirect the probe: the
  URL is authoritative from the task author, closing off a class of
  agent-driven exfil attempts that a `path`-taking variant would open.
- No new dispatch category — GENERIC already covers the pattern.
- Sits alongside `http_request` (allow-listed mock-web probe) and
  `calculator` (pure compute) as the third *no-per-invocation-args*
  reference in core, making the "config-driven, splatted into
  `__init__`" pattern easier to spot for future authors.

**Negative.**

- Adds one more entry to the union type of things `Dispatch.GENERIC`
  can point at. Judged acceptable — the registry is designed to grow
  along this axis.
- The tool cannot express "call the same peer service on N different
  paths within one trial." A benchmark that needs that would enable N
  copies of the tool under different names — currently unsupported by
  the registry (names are keys). If a real caller shows up, the
  registry can grow an alias mechanism. Not in scope today.

## Alternatives Rejected

**Option 2 — extend `http_request` with a no-arg preset.** Overloads
`http_request`'s meaning: today it is a *client-side URL constructor*
with an allow-list, and its schema advertises `method`/`url`/`headers`/
`json`. Adding a "no-args, config-driven URL" mode would fork the
schema at trial-registration time based on `tool_config`, which mocks
the "one tool, one schema" invariant `BuiltinGenericToolWrapper`
depends on. Cleaner as a distinct tool.

**Option 3 — keep it in the adapter.** Forces every future
code-migration adapter to re-implement the same HTTP wrapper, or to
depend on the Migration Bench adapter (an unrelated repo) for the base
class. Also lands adapter-specific code in core anyway the first time
we want to test the wrapper against the runner subset.

**Option 4 — agent-supplied path.** Rejected on the exfil concern: an
agent-supplied path lets the LLM aim the probe at whatever peer
endpoint it can enumerate. The reference-eval shape deliberately
denies this; the task author names the endpoint at pack-authoring
time.

## Testing

- `tests/unit/test_build_check_tool.py` — constructor validation,
  schema shape, successful invocation, HTTP-error passthrough,
  timeout/connect-error paths, kwargs-ignored contract. All HTTP
  calls mocked.
- `tests/unit/test_builtin_registry.py` — registry-lock: `build_check`
  is in `list_builtins()`, routes to `Dispatch.GENERIC`,
  `get_class("build_check")` returns `BuildCheckTool`.
- `tests/unit/test_builtin_generic_wrapper.py` — wrapper splats
  `tool_config` into `BuildCheckTool.__init__`; unknown `tool_config`
  keys fail loud (the first builtin whose `__init__` has a required
  kwarg, so this is where the splat contract earns its keep).
- `tests/canonical/test_runner_subset_partition.py` —
  `build_check.py` is in the runner subset closure (lazy-loadable
  path, alongside every other `builtin/` driver).

## Links

- ADR-0011 — Pattern-A: Protocol + entry-point registry + `InMemory*`
  fixture + canonical contract test.
- ADR-0018 — Multi-container under shared runtime (the compose-network
  peer-service context this tool consumes).
- ADR-0026 — Service-readiness contract (sibling pattern: a
  client-side probe against a `host:port` endpoint that surfaces the
  answer as structured data rather than a timeout).
- Issue #891 — filing for this tool.
