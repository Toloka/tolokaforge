# TypeSense Integration

TolokaForge ships the engine-side machinery for running TypeSense-backed search inside a benchmark run. This document describes the **engine/adapter split** for search and the **supported adapter-facing contract**.

## TL;DR

- **The engine owns**: the TypeSense container lifecycle (start, health-check, network-bridge, stop) and a set of small primitives adapters reuse (thread-safe domain-init coordination, result data classes).
- **Adapters own**: the *provider* — how documents are sourced, what a "domain" means for a given benchmark, and any benchmark-specific dependencies (schemas, registries, etc.).
- **The engine does not ship a real `TypeSenseClient` abstraction.** Adapters that need a client use the [`typesense`](https://pypi.org/project/typesense/) Python package directly (already a hard dependency of the engine).

This split exists because the per-benchmark indexing logic tends to drag in benchmark-specific dependencies (domain registries, schema generators, document loaders). Keeping the provider adapter-side prevents those dependencies from leaking into the public engine.

## The supported public contract

The names re-exported by `tolokaforge.core.search` are the **adapter-facing contract**. Removing or renaming one is a breaking change. The current shape:

| Symbol | Module | Purpose |
|---|---|---|
| `DomainState` | `tolokaforge.core.search.domain_state` | Per-domain state object with a thread-safe init claim. |
| `DomainStateManager` | `tolokaforge.core.search.domain_state` | Get-or-create registry over `DomainState`. |
| `DomainStatus` | `tolokaforge.core.search.domain_state` | Enum: `PENDING`, `INITIALIZING`, `READY`, `FAILED`. |
| `SearchResponse` | `tolokaforge.core.search.typesense` | Result envelope (hits, total, query, timing). |
| `SearchResult` | `tolokaforge.core.search.typesense` | Single hit (id, score, content, highlights). |

A contract test (`tests/unit/test_search_contract.py`) pins this set; CI fails if a name is removed or renamed without updating the contract.

The **container lifecycle primitives** are in `tolokaforge.core.search.typesense_server` (`TypeSenseServerManager`, `create_typesense_server`) and `tolokaforge.docker.stacks.typesense` (`typesense_service`). These are also part of the supported surface — adapters generally do not start the container themselves (the orchestrator does), but the manager is the canonical entry point for tests and for setups that bypass the orchestrator.

## Container lifecycle (engine-owned)

The orchestrator owns the TypeSense container per run:

1. On `tolokaforge run`, `Orchestrator._ensure_typesense_started()` calls `create_typesense_server(...)` → `TypeSenseServerManager.start()`.
2. The manager pulls the image, starts the container via the `tolokaforge.docker` foundation layer (`ServiceStack` + `typesense_service`), and waits for the health endpoint.
3. The manager bridges the container onto the Runner's Docker network so the Runner can reach it as `typesense:8108` via DNS.
4. On teardown, `Orchestrator.teardown()` calls `self._typesense_server.stop()` to remove the container and clean up the network bridge.

Adapters do not need to start the container themselves; the orchestrator passes the resolved `host`, `port`, and `api_key` to the adapter via run config.

## How to build a provider (adapter-side)

A provider is whatever the adapter needs to put on top of `core/search/*`. The engine offers two things you usually want to reuse:

1. **`DomainStateManager`** if the provider must coordinate concurrent initialization of the same domain (typical for thread-pooled trial runs).
2. **`SearchResponse` / `SearchResult`** if the provider exposes results in a structured form rather than passing raw `typesense` dicts up.

For the search client itself, use the [`typesense`](https://pypi.org/project/typesense/) package directly:

```python
import typesense

from tolokaforge.core.search import DomainStateManager, SearchResponse, SearchResult
from tolokaforge.core.search.typesense_server import TypeSenseServerManager


def build_client(host: str, port: int, api_key: str) -> typesense.Client:
    return typesense.Client({
        "nodes": [{"host": host, "port": port, "protocol": "http"}],
        "api_key": api_key,
        "connection_timeout_seconds": 5,
    })
```

A worked end-to-end example — start the manager, build the client, create a collection, index documents, search — lives in [`tests/integration/test_search_no_mcp_core.py`](../tests/integration/test_search_no_mcp_core.py). That test deliberately avoids importing anything benchmark-specific; it is the canonical reference for "what does the minimum integration look like."

## Concurrency: avoiding duplicate initialization

If multiple trials run concurrently and each initializes the same domain, you'll race on `create_collection`. The provider should coordinate via `DomainStateManager`:

```python
state, _created = manager.get_or_create(domain)
if state.claim_initialization():
    try:
        # only one thread reaches here per domain
        _index_documents(domain, client)
        state.set_ready()
    except Exception:
        state.set_failed()
        raise
state.wait_ready(timeout=30.0)
```

`DomainState` handles the `PENDING → INITIALIZING → READY/FAILED` state machine and the wait/notify around it.

## Why no engine-side `TypeSenseClient`

Prior versions of the engine shipped an abstract `TypeSenseClient` and a `TypeSenseStub` that silently returned empty results. We removed both:

- No real implementation existed in-tree, and no adapter implemented the abstract.
- The stub-returning-empty pattern violates the project's "honesty over convenience" stance — a search that silently returns no results corrupts evaluation rather than failing loudly.

Adapters that need a real client should use the `typesense` package directly. Adapters that need to test against a fake should mock the `typesense.Client` interface they actually use, not an engine-side abstract.

## Configuration

The orchestrator's TypeSense config is set per run:

```yaml
orchestrator:
  typesense:
    enabled: true
    mode: local          # "local" (engine starts a container) | "external"
    host: 127.0.0.1      # for "external" mode; ignored in "local"
    port: auto           # "auto" picks a free port and bridges to the runner net
    api_key: null        # null → engine generates one for "local"
    timeout: 30.0
```

Adapters read these from the run config and use them when constructing their provider/client.

## See also

- [`tests/integration/test_search_no_mcp_core.py`](../tests/integration/test_search_no_mcp_core.py) — end-to-end smoke test, no benchmark deps.
- [`tests/unit/test_search_contract.py`](../tests/unit/test_search_contract.py) — contract pin.
- [`tolokaforge/core/search/__init__.py`](../tolokaforge/core/search/__init__.py) — the exported surface.
- [`tolokaforge/core/search/typesense_server.py`](../tolokaforge/core/search/typesense_server.py) — container lifecycle manager.
