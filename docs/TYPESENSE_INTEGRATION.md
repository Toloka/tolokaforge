# TypeSense Integration

TolokaForge provides full TypeSense support for semantic search over knowledge base documents. This feature enables agents to search policy documents, knowledge bases, and other textual content using natural language queries.

## Overview

The engine's role is narrow: reach a TypeSense server, make it reachable from inside the runner container, and register a client there so a task's `search_policy` tools resolve. Indexing the documents is the job of the adapter that owns the corpus, host-side, before any trial runs.

- **Standalone Feature**: available to any adapter that declares a knowledge base
- **Automatic Indexing**: `docindex/*.md` documents are indexed host-side by the adapter that ships them; the runner recomputes the collection name from the same documents and attaches to the existing collection
- **Semantic Search**: vector and text-based search
- **Orchestrator-Managed Server**: automatic Docker container lifecycle management. A server that does not become ready within `timeout` aborts the run — the orchestrator raises with the address it tried and the reason, rather than continuing against an address nothing is listening on
- **Configurable**: via run config with auto port selection and API key generation
- **Fail loud**: a declared search plane that cannot be made to work stops the run or the trial rather than degrading to empty results. See [Failure semantics](#failure-semantics)

## Architecture

The address is a property of the stack, not of a task: the runner container is
created already knowing it, so nothing has to correct a task's connection
details after the fact.

```
 host                                    runner container
┌─────────────────────────────┐            ┌────────────────────────────┐
│ Orchestrator                │            │ RunnerServiceImpl          │
│                             │            │                            │
│ TypeSenseServerManager      │            │ RegisterTrial              │
│  - resolve port, api key    │            │  └ _init_typesense_for_    │
│  - start, wait_ready        │            │      trial                 │
│                             │            │    - reads docindex/*.md   │
│ service stack               │            │    - registers a client in │
│  - TYPESENSE_HOST/PORT      │            │      mcp_core's registry   │
│    on the runner            │            │                            │
│  - api key inside           │            │ search_policy tool         │
│    TOLOKAFORGE_SECRETS_JSON │            │  └ get_typesense_for_      │
│                             │            │      domain(domain)        │
│ bridge to runner-net        │            │                            │
│  - alias "typesense"        │            │                            │
│                             │            │                            │
│ Adapter (indexes the        │            │                            │
│ corpus before the run)      │            │                            │
└──────────────┬──────────────┘            └─────────────┬──────────────┘
               │                                         │
               │          ┌────────────────────┐         │
               └─────────►│ TypeSense server   │◄────────┘
    127.0.0.1:<port>      │ local or remote    │  TYPESENSE_HOST:PORT
                          └────────────────────┘
```

A server this process starts is bridged onto `runner-net` under the alias
`typesense`, and the runner is given `typesense:8108` — the container port,
which is fixed by the image. A `mode: remote` server is not ours to bridge, so
the runner is given the address the run config names — provided the runner can
reach it: a loopback host is refused at stack build (see
[Failure semantics](#failure-semantics)). Either way the host-side
address the orchestrator and the adapter index against never reaches the
container: inside `runner-net` it resolves to the runner itself.

The API key never becomes an environment variable of its own. It is registered
with the `SecretManager` as soon as it is resolved, which puts it in the
`TOLOKAFORGE_SECRETS_JSON` payload the runner container is built with and, in
the same move, in the log-redaction set.

## Implementation

### Core Components

1. **`tolokaforge/core/search/typesense_server.py`** — `TypeSenseServerManager` and `create_typesense_server`: Docker lifecycle for `mode: local`
2. **`tolokaforge/core/search/typesense.py`** — `TypeSenseClient` (abstract), `TypeSenseStub` (every search returns empty) and the `create_typesense_client` factory that hands it out: the search-backend interface. `TypeSenseStub` belongs to that adapter-facing surface — an adapter reaches it by asking for it. It is not a harness fallback: nothing on the runner's own path constructs one, and a trial whose real client cannot be registered is refused rather than served a stub
3. **`tolokaforge/core/search/domain_state.py`** — `DomainStateManager`: per-domain initialisation coordination, so concurrent tasks in one domain index once
4. **`tolokaforge/core/search/__init__.py`** — module exports

`typesense.py` and `domain_state.py` have no in-tree consumers. They are a supported interface for adapters maintained outside this repository — do not delete them on a grep ([#26](https://github.com/Toloka/tolokaforge/issues/26)).

### Document Loading

Documents live in a `docindex/` directory beside the domain's task cases:

```
domain/
├── docindex/
│   ├── order_management.md
│   ├── shipping_returns.md
│   └── customer_service.md
└── testcases/
    └── *.json
```

The adapter bundles `docindex/*.md` into the task's artifacts. The runner extracts them per trial and reads them back to compute the deterministic collection name (`<domain>_<sha256[:8]>`) — which is why every document must be readable: a corpus read that skipped one would address a collection the host-side indexer never created.

### Domain State Coordination

`DomainStateManager` gives one domain a single initialisation across concurrent tasks:

```python
from tolokaforge.core.search import DomainStateManager

manager = DomainStateManager()
state, created = manager.get_or_create("retail_domain")
if state.claim_initialization():
    # This caller owns indexing; others block in wait_ready().
    state.set_ready(document_count=12)
else:
    state.wait_ready(timeout=30.0)
```

The state machine is `PENDING → INITIALIZING → READY` on success, `→ FAILED` on error, and a failure is propagated to every waiter rather than retried silently.

## Where the runner reaches TypeSense

Two sources can name the address, and the runner resolves them once per
registration — in [`tolokaforge/runner/search_plane.py`](../tolokaforge/runner/search_plane.py):

1. **The stack** — `TYPESENSE_HOST` and `TYPESENSE_PORT` on the runner container, with the API key in the `SecretManager` (from `TOLOKAFORGE_SECRETS_JSON`), documented in the runner container's environment contract in [`STANDALONE_RUNNER.md`](STANDALONE_RUNNER.md#published-images).
2. **The task description** — the `host` / `port` / `api_key` of its `search` block.

The stack wins. A task's own details serve the runs where no stack set the
variables: a runner nobody started for this run (`auto_start_services: false`,
worker mode), and adapters that still emit an address of their own. If neither
source names a host, the run has no TypeSense plane.

**The runner reports which source answered.** Every log line and refusal that
names an address also names its basis — `stack_env` or `task_search_config` —
and that basis is the one the resolver returned, not a second reading taken at
the message site. An operator debugging a knowledge-base run reads the address
the client was actually built from, never a guess between two sources of truth.

**A half-declared stack address stops the trial.** `TYPESENSE_HOST` without
`TYPESENSE_PORT` (or either without the other, or a port that is not a number)
refuses registration naming both variables. Falling back to the task's details
would hand the trial the very address the stack was configured to replace —
host-side, and inside `runner-net` the runner itself.

An address with no API key is not an error. A `mode: remote` run registers no
key, and a server that does not require one is the server's own answer to give:
the binding carries `api_key=None` and client initialisation reports what the
server says.

## Runner-Side Client Registration

`RegisterTrial` registers a TypeSense client inside the runner container when **all three** conditions hold:

1. **The plane** — the plane serving this task's corpus is `typesense`.
2. **The corpus** — the task declares a knowledge base: `search.documents_path` is set.
3. **The address** — an address resolved, so the run has a TypeSense plane to serve it.

None of them is `search.enabled`. That flag means only "this task needs rag-service" and gates the separate RAG indexing block; a TypeSense-only domain sets `enabled: false` and still registers. All three are required, so a knowledge-base task in a TypeSense-disabled run does no TypeSense work and registers normally, a run with TypeSense configured does no TypeSense work for tasks that declare no knowledge base, and a rag corpus stays on its own plane in a run that offers both.

The TypeSense gate runs before the RAG gate. A task that declares both and whose TypeSense plane is broken reports the TypeSense failure.

### Which plane serves a corpus

`search.plane` is a fact about the task — `typesense` or `rag_service` — and it is what condition 1 reads. A task that declares none has its plane derived from the connection details it carries: a task naming a `host` is read as `typesense`. `rag_service` is never derived, only declared, so no run changes which rag work it does.

The runner reports which of the two happened alongside the address: every refusal names the plane and whether it was `declared` or `derived_from_connection_details`.

Two shapes are refused rather than registered:

- **The declaration and the bundle disagree** — a run configured a plane, the task declares no `documents_path`, and a `docindex/` corpus nonetheless arrived in its artifacts. Registering would hand the task a `search_policy` tool with nothing behind it.
- **No plane serves a declared corpus** — the task declares `documents_path`, an address resolved, nothing names or implies a plane, and `search.enabled` is false, so rag-service will not serve it either. This is what an adapter produces when it stops emitting connection details before it declares `search.plane`; the migration order that avoids it is in [`ADAPTER_INTERFACE.md`](ADAPTER_INTERFACE.md#searchconfigplane).

### Database Domain Assignment

For tools to access TypeSense, the database must have a domain attribute:

```python
# Create database and set domain for TypeSense access
db = InMemoryDatabase(additional_sources=additional_sources)
db.domain = self._domain_name  # Required for search_policy tools
```

## Tool Integration

TypeSense is typically used through `search_policy` tools:

### Example Tool Implementation

```python
from mcp_core.search import get_typesense_for_domain

class SearchPolicyTool(Tool):
    def _get_typesense_client(self, db: InMemoryDatabase):
        """Get TypeSense client for database domain."""
        domain = getattr(db, 'domain', None)
        return get_typesense_for_domain(domain) if domain else None
    
    async def run(self, db: InMemoryDatabase, request: SearchPolicyInput):
        client = self._get_typesense_client(db)
        results = client.universal_search_with_full_text(request.query, [])
        return SearchPolicyOutput(snippets=results[:request.max_results])
```

A tool reached by a registered trial can rely on the client being there: registration refuses when it is not, so the trial never starts. A tool that returns empty results instead of failing hides a dead search plane behind plausible-looking scores — the failure this document's [Failure semantics](#failure-semantics) exist to prevent.

## Failure semantics

A declared search plane that cannot be made to work stops the run or the trial. There is no stub fallback and no degraded mode: an agent that queries a dead knowledge base produces scores that read as measured behaviour.

The two tiers are deliberately different.

**Run-level (orchestrator) — a broken plane aborts the whole run, before any trial.**

- The local server does not become ready within `timeout`, or the Docker foundation layer is unavailable.
- The bridge onto `runner-net` cannot be built: no TypeSense container, no `runner-net`, no `orchestrator.typesense` block, or any Docker SDK failure.
- No server was started for this run and the configured `port` is still `"auto"` when the stack is built — `auto` is not an address, and nothing exists to resolve it.
- The address that would be injected into the runner container is a loopback host (`127.0.0.1`, `localhost`, `::1`) that no bridge replaces — inside the runner container a loopback address is the runner itself. Two configurations reach it: `mode: local` with both a pinned `port` and a pinned `api_key` (that shape skips the managed start, so nothing is bridged and the loopback `host` default would be injected verbatim), and `mode: remote` with `host` left at — or set to — a loopback. The message names the address and the remedy for the mode. A bridged local server never meets this refusal: it is injected as the network alias `typesense:8108`.

**Per-trial (runner) — a broken plane for one task refuses that trial's registration.**

`RegisterTrial` returns `success=false` with an error naming the trial, the domain, the plane and how it was resolved, the address tried and the source that named it. `Conductor._setup_trial` turns that into a trial failure before the agent loop, so a refused trial costs zero paid turns. Nine paths, in five classes:

| # | path | class |
|---|---|---|
| 1 | the runner image cannot provide a search client (the mcp_core search registry is not importable) | plane broken |
| 2 | the registry returns no client — the server is unreachable or refused the collection | plane broken |
| 3 | the registry raises; the underlying error is carried in the message | plane broken |
| 4 | the registry returns a client that reports the server as unavailable | plane broken |
| 5 | a `docindex/*.md` file cannot be read, so the collection name would not match the host-side index | corpus broken |
| 6 | the task declares a knowledge base but no readable `*.md` arrived in the trial's artifacts | corpus broken |
| 7 | a `docindex/` corpus arrived for a task declaring no `documents_path`, in a run that configured a plane | declaration and bundle disagree |
| 8 | the task declares a knowledge base, an address resolved, no plane serves it, and `search.enabled` is false | no plane serves a declared corpus |
| 9 | the stack set `TYPESENSE_HOST` without `TYPESENSE_PORT`, or either without the other, or a port that is not a number | the stack's address is half-declared |

Row 9 is resolved before the gate and refuses whatever the task declares — a half-configured stack is an operator error, and the task's own connection details are not a repair for it.

Rows 1–6 are reached only when all three conditions of the registration gate hold: the plane is `typesense`, the task declares a knowledge base (`search.documents_path`), and an address resolved. Row 6 is narrow by construction — reaching it already required `documents_path`, which an adapter sets only when a host-side `docindex/` exists, so it fires when a declared corpus failed to survive bundling and extraction.

Rows 7 and 8 are the shapes the gate would otherwise pass over in silence, and each is an adapter bug. In row 7 the bundle carries a corpus the declaration never asks for; either declare `documents_path` for the task, or stop bundling the corpus. In row 8 the corpus is declared and nothing serves it; declare `search.plane`. Both would otherwise register no client while reporting success, and every `search_policy` call in the trial would fail.

## Testing

| Lane | File | Locks |
|---|---|---|
| unit | `tests/unit/test_orchestrator_typesense_startup.py` | a server that never became ready aborts the run, and does not leave its would-be address in the config |
| unit | `tests/unit/test_orchestrator_typesense_bridge.py` | the Docker bridge either completes or aborts, and a completed bridge leaves the run config, the adapter and the description cache untouched |
| unit | `tests/unit/test_orchestrator_typesense_stack_address.py` | the injected address — the bridged alias, a verbatim remote address, one enablement predicate for every consumer, and the refusals of an unresolved `auto` port and of a loopback host the runner cannot reach |
| unit | `tests/unit/test_runner_search_plane_refusal.py` | the refusal paths reached through the gate, the gate ordering, the three shapes that must do no TypeSense work, and artifact cleanup on refusal |
| unit | `tests/unit/test_runner_typesense_address.py` | which source names the address, the basis reaching the message, and the half-declared stack refusal |
| unit | `tests/unit/test_runner_search_plane_declaration.py` | which plane serves a corpus, the derivation and its basis, and the refusal of a corpus no plane serves |
| unit | `tests/unit/test_runner_pipeline.py::TestRegisterTrialSearchPlanes` | the TypeSense and RAG planes stay decoupled |

## Server Configuration

TypeSense server can be configured in the run config YAML file under `orchestrator.typesense`:

### Configuration Options

```yaml
orchestrator:
  typesense:
    enabled: true          # Enable/disable TypeSense (default: true)
    mode: local            # "local", "remote", or "disabled"
    host: "127.0.0.1"      # TypeSense server host (default: 127.0.0.1)
    port: "auto"           # Port or "auto" for auto-selection (default: "auto")
    api_key: null          # API key (auto-generated if null for local mode)
    data_dir: ".cache/typesense"  # Data directory (default: .cache/typesense)
    image: "typesense/typesense:26.0"  # Docker image (local mode)
    container_name: "tolokaforge-typesense"  # Container name
    timeout: 30.0          # Connection timeout in seconds
    cleanup_on_exit: true  # Remove container on exit (local mode)
```

`host` must be reachable **from inside the runner container**, because that is where the address is injected. A loopback host (`127.0.0.1`, `localhost`, `::1`) is refused at stack build whenever it would be injected verbatim: `mode: remote`, or `mode: local` with both `port` and `api_key` pinned — that shape skips the managed start, so no server exists and nothing is bridged. A managed `local` server (`port: "auto"`, or an auto-generated key) is bridged onto the runner's network and injected as the alias `typesense:8108`, so its loopback default never reaches the runner.

### Mode Options

- **`local`**: Orchestrator manages a Docker container (auto start/stop)
- **`remote`**: Connect to an external TypeSense server. Nothing is started, but the address is real, so the connection details still reach the adapter. Requires a `host` the runner container can reach — the loopback default names no server and is refused
- **`disabled`**: no server is started, the stack is given no address and the orchestrator hands the adapter no connection details, so neither source names one and no task reaches the TypeSense plane

`enabled: false` and `mode: disabled` are equally final: either one stops the connection details, and a knowledge-base task in such a run registers normally with no search client.

### Example Configurations

#### Local Mode (Recommended for Development)

```yaml
orchestrator:
  typesense:
    mode: local
    port: "auto"  # Finds available port automatically
    # api_key auto-generated
```

#### Remote Mode (Production)

```yaml
orchestrator:
  typesense:
    mode: remote
    host: "typesense.example.com"
    port: 443
    api_key: "${TYPESENSE_API_KEY}"  # From environment variable
```

#### Disabled Mode

Either spelling stops the connection details reaching the adapter:

```yaml
orchestrator:
  typesense:
    enabled: false  # Or mode: disabled
```

## Deployment

### Development Setup (Manual)

If not using orchestrator-managed server:

1. **Start TypeSense Server**:
   ```bash
   docker run -d -p 8108:8108 \
     -v$(pwd)/typesense-data:/data \
     typesense/typesense:26.0 \
     --data-dir /data \
     --api-key=xyz \
     --listen-port 8108 \
     --enable-cors
   ```

2. **Set API Key**:
   ```bash
   export TYPESENSE_API_KEY=xyz
   ```

3. **Run Tests**:
   ```bash
   uv run tolokaforge run --config <your_run_config.yaml>
   ```

### Development Setup (Orchestrator-Managed)

With `mode: local`, the orchestrator handles everything automatically:

1. **Configure** `.cache/typesense` data directory (added to `.gitignore`)
2. **Start run** - TypeSense container starts automatically; if it never becomes ready, the run aborts before any trial
3. **Run completes** - Container is cleaned up (if `cleanup_on_exit: true`)

### Docker Networking

When the orchestrator manages the server (`mode: local`) and the run uses the engine's built-in stack, the TypeSense container joins `runner-net` under the alias `typesense` once that stack has started. That is the whole of the bridge.

The runner container was created knowing `typesense:8108` — inside a Docker network containers reach each other on the container port, never the host-mapped one — and joining the network is what makes that alias resolve. Nothing here touches `orchestrator.typesense`, the adapter's parameters, or the resolved task descriptions: the host-side address stays where the orchestrator and the adapter index against it.

A bridge that cannot be completed aborts the run. A missing TypeSense container, a missing `runner-net`, a missing `orchestrator.typesense` block, or any Docker SDK failure raises, naming both the alias the runner was handed and the host-side address the server answers on. There is no partial bridge: the alternative is a runner asking for an alias no network resolves, and every `search_policy` call in every trial failing.

A run whose tasks declare their own compose stack cannot be bridged at all — the TypeSense KB and a task-declared `environment_manifest` are mutually exclusive, and the orchestrator refuses that combination up front.

### Production Setup

For production, configure TypeSense server with:
- Persistent data volumes
- Proper API key management
- Network security
- Backup/restore procedures

## Server Management API

The `TypeSenseServerManager` class provides programmatic control:

```python
from tolokaforge.core.search.typesense_server import create_typesense_server

# Create server manager
server = create_typesense_server(
    port="auto",           # Auto-select available port
    api_key=None,          # Auto-generate API key
    data_dir=".cache/typesense",
    container_name="my-typesense",
)

# Start server
if server.start():
    print(f"TypeSense running on {server.host}:{server.port}")
    print(f"API Key: {server.api_key}")
    
    # ... use TypeSense ...
    
    # Stop server
    server.stop()

# Or use as context manager
with create_typesense_server() as server:
    # Server is running
    print(f"Port: {server.port}, Key: {server.api_key}")
# Server automatically stopped
```

## Troubleshooting

### Common Issues

1. **The run aborts with `orchestrator.typesense: the local TypeSense server … never became ready`**:
   - Check Docker container status and the container's logs: `docker ps`, `docker logs tolokaforge-typesense`
   - Raise `orchestrator.typesense.timeout` if the container is slow to answer its probes
   - "no port was ever resolved" in place of an address means the failure came before port selection — the Docker foundation layer was unimportable

2. **"Forbidden" API key errors**:
   - Set TYPESENSE_API_KEY environment variable
   - Ensure key matches server configuration

3. **"Database has no domain" errors**:
   - Ensure adapter sets `db.domain` attribute
   - Check that domain name is properly inferred

4. **A trial is refused with `the knowledge base is unusable` or `the search declaration and the artifact bundle disagree`**:
   - Verify the adapter bundles `docindex/*.md` into the task's artifacts, and that every one of them is readable UTF-8
   - "did not survive bundling" means the task declared `documents_path` and no readable `*.md` arrived — an adapter-side bundling bug
   - "disagree" means the opposite: a corpus arrived for a task declaring no `documents_path`. Declare it, or stop bundling the corpus

5. **Docker not available**:
   - Docker SDK error: Install with `uv add docker`
   - Docker daemon not running: Start Docker service
   - Permission issues: Ensure user has Docker access

6. **Port conflicts**:
   - Use `port: "auto"` to auto-select available port
   - Check for running TypeSense containers: `docker ps | grep typesense`

7. **Container cleanup issues**:
   - If container is not removed, manually clean up: `docker rm -f tolokaforge-typesense`

### Logging

Enable debug logging to troubleshoot issues:

```python
import logging
logging.getLogger("tolokaforge.core.search.typesense_server").setLevel(logging.DEBUG)
logging.getLogger("tolokaforge.core.search.domain_state").setLevel(logging.DEBUG)
logging.getLogger("mcp_core.search").setLevel(logging.DEBUG)
```
