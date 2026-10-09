# 0057. Carry `mutates_state` on the tool wire schema, declared by the tool's author

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`ToolSchema.category` (`read`/`write`/`compute`) is set to `"compute"` by every
adapter, and nothing reads it except two pass-throughs. The platform does
depend on one tool property: does a call change the graded state? That
property lives only in adapter- or domain-local code:

- τ²'s `@is_tool(mutates_state=)` drives replay inside the τ³ host;
- the frozen adapter infers a write surface by static analysis;
- the native `DomainToolRegistry` has no marker at all, so state sync and
  golden replay re-execute every call.

A shared tool library (ADR-0056) brings hundreds of tools whose `mutates_state`
is known and tested. Without a wire field the engine cannot replay only the
mutating calls, report read/write metrics, run a read-only trial, or reuse a
library's read tools for the judge.

This ADR adds the field only. Each consumer of the flag is its own later
change. It is independent of ADR-0056 and ADR-0058.

## Decision Drivers

- One source of truth, declared by the tool's author and forwarded by every adapter.
- No behaviour change for existing packs: an optional field with a conservative default.
- Declared where the protocol already has a word for it.

## Considered Options

1. **Keep `category`, add `mutates_state: bool | None`.** One meaningful field
   next to a dead one.
2. **Replace `category` with `mutates_state`.** A wire break for every adapter
   in one step.
3. **A rich taxonomy on the wire** (effect kind, tables, idempotency). Nothing
   consumes it.

## Decision

We will adopt **Option 1** now, and retire `category` in a later release once
every adapter has moved.

- `ToolSchema.mutates_state: bool | None = None`. `None` means unknown and
  reads as `True` wherever the engine needs an answer, which is today's
  behaviour.
- The field is left out of the serialised schema while it is `None`. An
  engine image that predates the field therefore accepts every pack that does
  not set it.
- MCP servers declare the flag through the protocol's own tool annotation,
  `readOnlyHint`. `DomainToolRegistry.tool(description, mutates_state=…)` sets
  the annotation. The native adapter's `tools/list` introspection reads it back
  into the schema and into the `fixtures/tools.json` cache. A server written
  for another engine declares the same annotation the same way. A tool that
  declares nothing gets no annotation.
- Adapters that know the flag from their own sources (τ³ from
  `__mutates_state__`) set it directly. Nothing reads `category` for it.

Consumers follow in their own changes, each with its own review: replay and
state sync that skip non-mutating calls, read/write metrics, a read-only trial
mode.

## Consequences

### Positive

- A library declares the flag once, and every surface forwards it. The engine
  can start consuming it without another wire change.
- No pack changes: a tool that declares nothing behaves as before.

### Negative / Trade-offs

- A wrongly declared `False` would drop a write from a replay that trusts the
  flag. The library's purity checks and the db-service diff path, which does
  not depend on the flag, mitigate this. Until a consumer trusts the flag,
  nothing changes at run time.

### Follow-ups

- Code changes required: the `ToolSchema` field and its conditional
  serialisation; the `mutates_state` argument and annotation in
  `DomainToolRegistry.tool`; reading the annotation in the native adapter's
  introspection and its cache; the τ³ adapter forwarding `__mutates_state__`;
  later, the deprecation of `category`.
- Documentation to update: `TASK_DESCRIPTION_SCHEMA.md`, `MCP_INTEGRATION.md`.
- Tests to add: the annotation's round trip through a real MCP server; an
  undeclared tool serialises without the field.

## Links

- Related ADRs: [0017](0017-tool-lifecycle.md), [0056](0056-shared-tool-libraries-through-tool-artifacts.md).
- Related code: `runner/models.py::ToolSchema`, `core/tools_interface.py::DomainToolRegistry.tool`, `adapters/_task_loader.py::_fetch_mcp_tool_schemas`.
- External references: MCP tool annotations (`readOnlyHint`).
