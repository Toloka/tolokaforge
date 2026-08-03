# Native Format Conversion Layer

> **Stage 5** of the [Future Development Plan](FUTURE_DEVELOPMENT.md).

## Overview

The conversion layer allows an external task format — read by an installed
conversion adapter — to be converted into native TolokaForge format on disk.
This enables:

1. **Debuggability** — inspect exactly what the orchestrator sees for any task.
2. **Caching** — pre-generate converted tasks to avoid runtime adapter loading.
3. **Consistency** — all tasks end up as standard `task.yaml` + `grading.yaml`.
4. **Testing** — validate and test against the canonical native format.
5. **Decoupling** — after conversion, no runtime dependency on external parsers.

## CLI Usage

```bash
# Convert an external task set to native format using an installed
# conversion adapter (registered via the tolokaforge.adapters entry-point group)
tolokaforge adapter convert \
    --name <adapter-name> \
    --tasks-glob "path/to/source/tasks/**" \
    --output converted/example/

# With validation (checks that converted task.yaml is parsable as TaskConfig)
tolokaforge adapter convert \
    --name <adapter-name> \
    --tasks-glob "path/to/source/tasks/**" \
    --output converted/example/ \
    --validate
```

### Options

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Adapter name (`native`, `terminal_bench`, or an installed conversion adapter) |
| `--tasks-glob` | Yes | Glob pattern for source tasks (interpretation is adapter-specific) |
| `--output` | Yes | Output directory |
| `--adapter-params` | No | JSON string of extra adapter params |
| `--validate` | No | Run validation pass on converted output |
| `--verbose` | No | Enable debug logging |

## Output Format

Each converted task produces a directory:

```
{output}/{task_id}/
├── task.yaml                  # TaskConfig — the task definition
├── grading.yaml               # GradingConfig — grading rules
├── initial_state.json         # Initial database / environment state
├── system_prompt.md           # System prompt (wiki) text
└── fixtures/
    ├── tools.json             # Tool schemas (name, description, parameters)
    ├── golden_actions.json    # Expected tool-call sequence for grading
    ├── unstable_fields.json   # Fields to exclude from hash comparison
    └── metadata.json          # Adapter-specific provenance and hints
```

### File Details

#### `task.yaml`

Standard TolokaForge TaskConfig with file references:

```yaml
task_id: "example-001"
name: "Example Task"
category: "retail"
description: "Customer wants to return an order"
initial_state:
  json_db: "initial_state.json"
tools:
  agent:
    enabled: ["get_order", "cancel_order", "refund_order"]
  user:
    enabled: []
user_simulator:
  mode: "llm"
  persona: "customer"
  backstory: "..."
grading: "grading.yaml"
system_prompt: "system_prompt.md"
```

#### `grading.yaml`

Standard GradingConfig:

```yaml
combine:
  method: weighted
  weights:
    state_checks: 1.0
  pass_threshold: 1.0
state_checks:
  hash:
    enabled: true
    weight: 1.0
    golden_actions:                      # or expected_state_hash
      - name: place_order
```

#### `fixtures/tools.json`

Array of tool schema objects (schema-only — no runtime tool wrappers):

```json
[
  {
    "name": "get_order",
    "description": "Look up order details by order ID",
    "parameters": {
      "type": "object",
      "properties": {
        "order_id": {"type": "string"}
      },
      "required": ["order_id"]
    }
  }
]
```

#### `fixtures/golden_actions.json`

Expected tool-call sequence used for hash-based grading:

```json
[
  {
    "tool_name": "cancel_order",
    "arguments": {"order_id": "ORD-123"}
  }
]
```

#### `fixtures/unstable_fields.json`

Fields excluded from state hash comparison (auto-IDs, timestamps):

```json
[
  {
    "table_name": "orders",
    "field_name": "updated_at",
    "reason": "timestamp"
  }
]
```

## Python API

### NativeTaskBundle

```python
from tolokaforge.adapters.base import NativeTaskBundle

bundle = NativeTaskBundle(
    task_config={...},       # dict → task.yaml
    grading_config={...},    # dict → grading.yaml
    initial_state={...},     # dict → initial_state.json
    system_prompt="...",     # str  → system_prompt.md
    fixtures={...},          # dict → fixtures/ directory
    metadata={...},          # dict → fixtures/metadata.json
)
```

### convert_to_native()

```python
adapter = get_adapter("<adapter-name>", {"tasks_glob": "path/to/source/**"})
task_ids = adapter.get_task_ids()
bundle = adapter.convert_to_native(task_ids[0])
```

### write_bundle()

```python
from tolokaforge.adapters.bundle_writer import write_bundle

task_dir = write_bundle(bundle, output_dir=Path("converted"), task_id="task-001")
```

## Tool Conversion Limitations

The conversion layer extracts **tool schemas only** — it does not produce
runtime tool wrappers.  Converted `fixtures/tools.json` contains the
name/description/parameters for each tool, but the actual tool implementation
stays in the adapter backend that owns the tool implementations.

This means:

- Converted tasks can be **inspected**, **validated**, and **compared**.
- To **execute** converted tasks, you still need the original adapter runtime
  or a compatible MCP server that implements the tool schemas.

## Supported Adapters

| Adapter | Source Format | Notes |
|---------|--------------|-------|
| `native` | Already native | `convert_to_native()` raises `NotImplementedError` |

Conversion-capable adapters are distributed as separate installable plugins,
registered via the `tolokaforge.adapters` entry-point group. Install one to
convert its source format; an adapter that produces shared, cross-task resources
emits them by overriding `BaseAdapter.write_shared_resources()`.

## See Also

- [Adapter Interface Contract](ADAPTER_INTERFACE.md) — full adapter API
- [Adapter Architecture](ADAPTER_ARCHITECTURE.md) — plugin discovery
- [Future Development](FUTURE_DEVELOPMENT.md) — Stage 5 roadmap
