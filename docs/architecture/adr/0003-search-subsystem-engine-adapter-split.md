# 0003. Search subsystem — engine owns container + primitives, adapters own provider

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** @cirogam22
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine ships modules under `tolokaforge/core/search/` and a TypeSense
Docker stack under `tolokaforge/docker/stacks/typesense.py`. When the engine
was open-sourced, the existing `typesense_provider.py` was deleted because it
imported a benchmark-specific package that was not shippable publicly. The
provider got re-homed into an adapter; the engine kept the lower-level
primitives.

The split was correct but **undocumented**. Three consequences followed:

1. A subsequent silent breakage in v0.2.1 — the adapter's `ImportError`
   fallback masked a deleted symbol, search returned empty results across
   every search-dependent task, and the failure surfaced only after eval
   degradation was investigated downstream.
2. `docs/TYPESENSE_INTEGRATION.md` and the docstring example in
   `tolokaforge/core/search/typesense_server.py` referenced the deleted
   provider as if it still existed.
3. `tolokaforge.core.search.__init__` exposed an abstract
   `TypeSenseClient` and a `TypeSenseStub` returning empty results — an
   "honesty over convenience" violation per [`docs/architecture/README.md`
   §2](../README.md#2-constraints), since nothing implemented the abstract
   and the stub silently lied about search.

The 0.3.0 exit criterion is *"a TypeSense search task must run with no
benchmark-specific package installed"*. That makes the split a load-bearing
contract, not an incidental layout. It needs to be pinned.

## Decision Drivers

- **Honesty over convenience.** No silent fallbacks; no defaults that hide
  configuration mistakes (overview §2).
- **Adapter plugin discipline.** Adapter-specific dependencies must not leak
  into the public engine (overview §4).
- **Stable adapter boundary.** Adapter authors must be able to depend on a
  named, tested public surface; renames must fail CI in the engine rather
  than silently downstream.
- **Public verifiability.** The "no benchmark-specific package required"
  property must be exercised in the public CI suite, not just asserted.

## Considered Options

1. **Re-introduce a generic `TypeSenseProvider` in the engine.** Lift the
   benchmark-independent parts back into `core/search/`. The engine grows a
   second abstraction adapter authors must learn; the boundary it draws is
   one that *might* generalise, not one that has.
2. **Contract pin only.** Document the existing primitives as the public
   surface, fix stale references, leave providers entirely adapter-side.
   Smallest possible change.
3. **Contract pin + public verification (this ADR).** Option 2, plus a
   public smoke test that drives the contract end-to-end without any
   benchmark-specific imports, so the exit-bar is verified by CI rather
   than asserted in prose.

## Decision

Adopt **Option 3**.

- The engine owns container lifecycle (`TypeSenseServerManager`,
  `create_typesense_server`, `tolokaforge.docker.stacks.typesense`) and a
  small set of primitives adapters reuse: thread-safe domain-init
  coordination (`DomainState`, `DomainStateManager`, `DomainStatus`) and
  the result envelopes (`SearchResponse`, `SearchResult`).
- The engine deliberately does **not** ship a `TypeSenseClient`
  abstraction. Adapters that need a client use the `typesense` package
  directly — already a hard engine dependency. The previously-exported
  `TypeSenseClient` / `TypeSenseStub` / `create_typesense_client` are
  removed.
- The exported surface is the contract. A contract test
  (`tests/unit/test_search_contract.py`) pins it and asserts the removed
  abstractions do not re-surface.
- An integration smoke test (`tests/integration/test_search_no_mcp_core.py`)
  drives the public surface end-to-end with zero benchmark-specific
  imports, gating the 0.3.0 exit criterion in CI.
- The engine/adapter boundary is documented in
  [`docs/TYPESENSE_INTEGRATION.md`](../../TYPESENSE_INTEGRATION.md), which
  describes the supported surface, the container-lifecycle ownership, the
  concurrency primitive, and the canonical reference for a working
  integration.

## Consequences

### Positive

- The contract is loud: removing or renaming a public symbol fails CI in
  the engine, not silently in some downstream adapter.
- The exit bar is verifiable. A reviewer can run one test and confirm the
  engine has no hidden benchmark-specific dependency in the search path.
- Adapter authors have a documented, minimal, stable seam to build on
  without negotiating with the engine team about provider shape.
- The "no silent fallbacks" rule is upheld — there is no engine-side stub
  pretending to be a real client.

### Negative / Trade-offs

- Adapter authors must own a small amount of additional code (their
  provider + their TypeSense client setup) compared to a hypothetical
  "engine ships a default provider" world. We judged this acceptable
  because the per-benchmark indexing logic varies enough that an engine
  default would either be too thin to be useful or would re-import the
  benchmark-specific dependencies we're trying to keep out.
- Removing `TypeSenseClient` / `TypeSenseStub` is technically a breaking
  change for any external consumer that imported them. Grep across the
  workspace shows zero in-repo callers; external consumers, if any, must
  switch to using the `typesense` package directly.

### Follow-ups

- Code changes required: `tolokaforge/core/search/__init__.py`
  (slim `__all__`), `tolokaforge/core/search/typesense.py` (delete
  abstract + stub + factory; keep data classes),
  `tolokaforge/core/search/typesense_server.py` (replace stale docstring
  example).
- Documentation to update: `docs/TYPESENSE_INTEGRATION.md` (full rewrite),
  `docs/architecture/README.md` §4 (add Search subsystem row).
- Tests to add: `tests/unit/test_search_contract.py`,
  `tests/integration/test_search_no_mcp_core.py`.

## Links

- Related ADRs:
  [0001 — Record architecture decisions in ADRs](0001-record-architecture-decisions.md),
  [0002 — External model registry](0002-external-model-registry.md) (same
  externalisation discipline applied to a different layer).
- Related code:
  - [`tolokaforge/core/search/__init__.py`](../../../tolokaforge/core/search/__init__.py) — the exported surface.
  - [`tolokaforge/core/search/typesense_server.py`](../../../tolokaforge/core/search/typesense_server.py) — container lifecycle.
  - [`tolokaforge/core/search/domain_state.py`](../../../tolokaforge/core/search/domain_state.py) — concurrency primitives.
- Documentation:
  [`docs/TYPESENSE_INTEGRATION.md`](../../TYPESENSE_INTEGRATION.md).
