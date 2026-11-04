# 0067. A tool's declared refusal is its own tool-call status

- **Status:** Accepted
- **Date:** 2026-11-03
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`DomainToolRegistry` documents `ToolError` as the way a tool signals a business-logic
failure: an order that does not exist, a request the vendor API answered with 404. Over
MCP it caught the exception and *returned* `{"error": ..., "details": [...]}` as an
ordinary answer, so the runner recorded the call as `success`. Grading already reads that
payload as a failure (`golden_replay.declared_failure`), but the recorded status, the
transcript, trace checks on `status:`, `required_tools` and anything else reading the
tool-call record saw a success. Every native pack is affected, including those serving a
shared tool library through the registry.

The flag alone does not fix it. An `isError` reply without a category is recorded as
`environment_error` — over MCP, the status of FastMCP refusing the arguments against the
tool's signature and of an exception escaping the tool. Folding a tool's deliberate
refusal into it would erase the distinction the canonical shop-orders suite has held
since the trace-checks milestone: the tool ran and declined, versus the call failing.

## Decision Drivers

- A refusal the tool declared is a failed call on the record.
- It stays apart from an untyped `isError` (`environment_error`), from a refusal of the
  arguments (`invalid_arguments`) and from a runner or transport failure (`error`).
- The agent reads exactly what it read before.
- Golden replay classifies the refusal identically on both substrates.

## Considered Options

1. **A new status, `tool_error`**, declared through the `_meta` category channel that
   `invalid_arguments` already uses.
2. **The category mapped to the existing `error`.** The refusal would merge with the
   runner's own failures, and the agent would read an `Error: ` prefix it never read.
3. **An untyped `isError`.** The refusal would merge with argument-validation failures
   and crashes inside the tool.

## Decision

We will adopt **Option 1**.

- `DomainToolRegistry` answers a `ToolError` with `isError: true`, `_meta`
  `{"tolokaforge/error_category": "tool_error"}`, and the same text FastMCP renders for the
  payload returned as a value. Tools register without structured output, because FastMCP
  validates a returned `CallToolResult` against the output schema it would derive; the
  engine reads only the text content. That needs `mcp>=1.19`, the first whose FastMCP
  passes a returned `CallToolResult` through. `TOOLS[name].invoke` (grading mode) still
  returns the payload.
- The categories live in `core.mcp_error_categories`, a module with no imports, read by
  the registry, the runner and any third-party server.
- The runner records the category as `EXECUTION_STATUS_TOOL_ERROR` /
  `ToolExecutionStatus.TOOL_ERROR`. Like `environment_error`, it is a completed call: the
  agent reads the response verbatim, the record keeps it as the output, and the
  environment-error budget counts it. Unlike `invalid_arguments`, which the budget leaves
  out because the caller can correct its input, a refusal is the environment's answer to
  a well-formed request — what the budget's "completed with an error flag" and the
  τ-lineage error budgets count.
- Golden replay on the runner reads a `tool_error` reply as a reported failure, from the
  same payload core reads off `invoke` (an empty `{"error": ""}` is no failure on either).
- Failure attribution does not take a refusal for a tool-execution failure.
- `ENGINE_PROTOCOL_VERSION` moves to 7: an older runner image ships its own registry and
  records the refusal as before, and an older host could not record the new status.
- `ToolExecutionStatus` gains a value in the trial contract; this shares ADR-0066's bump
  of `TRIAL_CONTRACT_VERSION` to 2, so the two ship in one release.

## Consequences

### Positive

- The record, trace checks, the transcript judge and failure analysis see a declared
  refusal as one, on every native pack, with no pack change.
- Third-party MCP servers can declare the same category.

### Negative / Trade-offs

- A call that raised `ToolError` was recorded `success` and now is `tool_error`, so
  everything counting successes changes for it: a `status:` matcher expecting `success`,
  `required_tools` / `required_actions` whose only call was refused, `tool_success_rate`,
  the `tool_usage` success/error counts and corpus curation. A task with
  `max_environment_errors` spends its budget on refusals.
- On a tool annotated with a non-mapping return (`-> list[...]`, `-> str`, a model), a
  `ToolError` used to fail FastMCP's output validation and reach the agent as `Error
  executing tool ...`, recorded `environment_error`; it now reaches it as the payload,
  recorded `tool_error`. A tool returning a value that violates its own annotation is no
  longer refused by that validation.
- `tools/list` no longer carries an output schema for registry tools, and an answer
  FastMCP renders as no content (`None`, an empty list) reaches the agent as
  `{"content": [], "isError": false}` without `structuredContent`.
- An engine and runner image below protocol 7 refuse to pair.

## Links

- Related ADRs: [0061](0061-mutates-state-on-the-tool-wire.md), [0066](0066-judge-evidence-parts.md).
- Related code: `core/tools_interface.py` (`DomainToolRegistry`, `ToolError`),
  `core/mcp_error_categories.py`, `runner/tool_factory.py` (`_declared_category`),
  `runner/service.py`, `runner/protocol.py`, `tools/registry.py` (`ToolExecutionStatus`,
  `ENVIRONMENT_ERROR_STATUSES`), `core/failure_attribution.py`.
- Documentation: `MCP_INTEGRATION.md` § Declaring a failure, `GRPC_PROTOCOL.md` § Version
  lock, `OUTPUT_FORMAT.md`, `TASKS.md`, `ADAPTER_INTERFACE.md`, `GRADING.md`.
