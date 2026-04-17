# TypeSense Integration

TolokaForge provides full TypeSense support for semantic search over knowledge base documents. This feature enables agents to search policy documents, knowledge bases, and other textual content using natural language queries.

## Overview

The TypeSense integration bridges TolokaForge adapters with the `mcp_core` TypeSense infrastructure:

- **Standalone Feature**: Can be used by any adapter (Native, internal MCP JSON, Tau)
- **Automatic Indexing**: Documents in `docindex/` directories are automatically indexed
- **Semantic Search**: Supports both vector and text-based search
- **Graceful Degradation**: Falls back to stub behavior when TypeSense server is unavailable
- **Orchestrator-Managed Server**: Automatic Docker container lifecycle management
- **Configurable**: Via run config with auto port selection and API key generation

## Architecture

```
┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────────┐
│   Adapter       │    │  TypeSenseProvider   │    │   mcp_core          │
│                 │    │                      │    │                     │
│ _init_typesense │────┤ ensure_domain_init   ├────┤ TypesenseIndex     │
│ (cached)        │    │ (coordinated)        │    │ universal_search_*  │
│                 │    │ search               │    │                     │
└─────────────────┘    └──────────────────────┘    └─────────────────────┘
         │                      │
         │                      ▼
         │             ┌──────────────────────┐
         │             │  DomainStateManager  │
         │             │  - Per-domain state  │
         │             │  - Thread-safe       │
         │             │  - Wait coordination │
         │             └──────────────────────┘
         │                      │
         ▼                      ▼
       ┌────────────────────────────────────────────────┐
       │           TypeSense Server                      │
       │                                                │
       │  ┌──────────────────────────────────────────┐  │
       │  │ TypeSenseServerManager (local mode)       │  │
       │  │ - Auto port selection                    │  │
       │  │ - Docker foundation layer (ServiceStack) │  │
       │  │ - Auto API key generation                │  │
       │  └──────────────────────────────────────────┘  │
       │                    OR                          │
       │  ┌──────────────────────────────────────────┐  │
       │  │ External TypeSense (remote mode)         │  │
       │  │ - Pre-configured server                  │  │
       │  └──────────────────────────────────────────┘  │
       └────────────────────────────────────────────────┘
```

## Implementation

### Core Components

1. **`tolokaforge/core/search/typesense_provider.py`** - Main TypeSense provider with domain-level caching
2. **`tolokaforge/core/search/domain_state.py`** - Domain state management for coordinated initialization
3. **`tolokaforge/core/search/typesense.py`** - Stub interface (backward compatibility)
4. **`tolokaforge/core/search/__init__.py`** - Module exports

### Provider Configuration

```python
from tolokaforge.core.search.typesense_provider import create_typesense_provider

provider = create_typesense_provider(
    enabled=True,              # Enable/disable TypeSense
    host="127.0.0.1",         # TypeSense server host
    port=8108,                # TypeSense server port
    api_key=None,             # API key (uses TYPESENSE_API_KEY env var if None)
    timeout=30.0,             # Connection timeout
    use_stub=False            # Force stub implementation
)
```

### Document Loading

Documents are automatically loaded from `docindex/` directories:

```
domain/
├── docindex/
│   ├── order_management.md    # ← Indexed automatically  
│   ├── shipping_returns.md    # ← Indexed automatically
│   └── customer_service.md    # ← Indexed automatically
└── testcases/
    └── *.json
```

### Search Interface

```python
# Initialize for domain
success = provider.initialize_for_domain("retail_domain", documents)

# Search documents  
response = provider.search(
    domain="retail_domain",
    query="how to return a product", 
    max_results=5
)

# Response format
{
    "hits": [
        {
            "document_id": "returns_policy",
            "score": 0.95,
            "content": {
                "source": "returns_policy.md",
                "text": "To return a product...",
                "vector_distance": 0.12
            }
        }
    ],
    "total_hits": 3,
    "query": "how to return a product",
    "search_time_ms": 15.0
}
```

## Internal MCP Integration

The internal MCP adapter automatically initializes TypeSense during environment creation with **domain-level caching**:

### Domain-Level Caching

TypeSense initialization is cached per domain to avoid redundant document indexing when running multiple tasks from the same domain:

- **Provider Caching**: A single `TypeSenseProvider` instance is cached per adapter
- **Domain State Coordination**: Uses `DomainStateManager` to ensure only one initialization per domain
- **Thread-Safe**: Multiple concurrent task executions safely share the cached state

```python
# In the internal MCP adapter
def _get_typesense_provider(self) -> Optional[TypeSenseProvider]:
    """Get or create TypeSense provider (cached at adapter level)."""
    with self._provider_lock:
        if self._typesense_provider is None:
            self._typesense_provider = create_typesense_provider()
        return self._typesense_provider

def _init_typesense(self, task_id: str) -> None:
    """Initialize TypeSense for knowledge base search (domain-level caching)."""
    provider = self._get_typesense_provider()
    if provider is None:
        return
    
    # ensure_domain_initialized handles coordination:
    # - Only the first caller does actual initialization
    # - Subsequent callers wait for completion and reuse the result
    provider.ensure_domain_initialized(
        domain=self._domain_name,
        docindex_path=self._domain_path / "docindex",
    )
```

### Domain State Lifecycle

The domain initialization follows a state machine:

```
PENDING → INITIALIZING → READY (success)
                      → FAILED (error)
```

- **PENDING**: Initial state, domain not yet initialized
- **INITIALIZING**: First task is indexing documents
- **READY**: Documents indexed, subsequent tasks skip re-indexing
- **FAILED**: Initialization failed, error is propagated

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
        if client:
            results = client.universal_search_with_full_text(request.query, [])
            return SearchPolicyOutput(snippets=results[:request.max_results])
        else:
            return SearchPolicyOutput(snippets=[])  # Fallback
```

## Testing

### Unit Tests

```python
# tests/unit/test_typesense_provider.py
def test_document_loading():
    provider = create_typesense_provider(use_stub=True)
    documents = provider.load_documents_from_directory(docs_dir)
    assert len(documents) > 0

def test_search_functionality():
    provider = create_typesense_provider(use_stub=True) 
    provider.initialize_for_domain("test", ["sample document"])
    response = provider.search("test", "sample query")
    assert response.total_hits >= 0
```

### Functional Tests

```python
# tests/functional/test_internal_mcp_typesense.py
def test_internal_mcp_typesense_integration():
    adapter = InternalMcpAdapter(params)
    task_config = adapter.get_task("TC-001")
    env = adapter.create_environment("TC-001")
    
    # Verify TypeSense tools work
    tools = adapter.get_registry_tools("TC-001", env)
    search_tool = next(t for t in tools if "search_policy" in t.name)
    result = search_tool.execute(query="test query")
    assert result.success
```

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

### Mode Options

- **`local`**: Orchestrator manages a Docker container (auto start/stop)
- **`remote`**: Connect to an external TypeSense server
- **`disabled`**: TypeSense is disabled, search_policy returns empty results

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
   uv run tolokaforge run --config my_run_config.yaml
   ```

### Development Setup (Orchestrator-Managed)

With `mode: local`, the orchestrator handles everything automatically:

1. **Configure** `.cache/typesense` data directory (added to `.gitignore`)
2. **Start run** - TypeSense container starts automatically
3. **Run completes** - Container is cleaned up (if `cleanup_on_exit: true`)

### Production Setup

For production, configure TypeSense server with:
- Persistent data volumes
- Proper API key management
- Network security
- Backup/restore procedures

## Server Management API

The `TypeSenseServerManager` class provides programmatic control:

```python
from tolokaforge.core.search.typesense_server import (
    TypeSenseServerManager,
    create_typesense_server,
    find_free_port,
    generate_api_key,
)

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

1. **"Connection refused" errors**:
   - Ensure TypeSense server is running on 127.0.0.1:8108
   - Check Docker container status: `docker ps`

2. **"Forbidden" API key errors**:
   - Set TYPESENSE_API_KEY environment variable
   - Ensure key matches server configuration

3. **"Database has no domain" errors**:
   - Ensure adapter sets `db.domain` attribute
   - Check that domain name is properly inferred

4. **Empty search results**:
   - Verify documents exist in `docindex/` directory
   - Check TypeSense initialization logs
   - Confirm documents are .md files with content

5. **Docker not available**:
   - Docker SDK error: Install with `pip install docker` or `uv add docker`
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
logging.getLogger("tolokaforge.core.search.typesense_provider").setLevel(logging.DEBUG)
logging.getLogger("tolokaforge.core.search.typesense_server").setLevel(logging.DEBUG)
logging.getLogger("mcp_core.search").setLevel(logging.DEBUG)
```
