# NativeAdapter — Complete Reference

Built-in adapter for file-based TolokaForge tasks (`task.yaml` + `grading.yaml`).
Default adapter: used automatically when no `harness_adapter` is specified in `run.yaml`.

For adapter architecture and interface contracts see
[ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) and
[ADAPTER_INTERFACE.md](ADAPTER_INTERFACE.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Task Directory Layout](#task-directory-layout)
3. [task.yaml Schema](#taskyaml-schema)
4. [grading.yaml Schema](#gradingyaml-schema)
5. [Tool Schemas (fixtures/tools.json)](#tool-schemas-fixturestoolsjson)
6. [How the Adapter Works](#how-the-adapter-works)
7. [Docker Execution](#docker-execution)
8. [Run Configuration](#run-configuration)
9. [Known Issues](#known-issues)

---

## Overview

`NativeAdapter` loads tasks from plain YAML files on disk.  It is the default
adapter for tasks authored directly in the TolokaForge repository and does not
require any external plugins.

Tools are provided by a task-local `mcp_server.py` script that the adapter
references from `task.yaml`.  The runner (local or Docker) starts the MCP
server as a subprocess and exposes its tools to the agent via the standard
tool registry.

**Key properties:**

| Property | Value |
|----------|-------|
| Adapter key | `native` (built-in, no install needed) |
| Task detection | Glob pattern matching `**/task.yaml` |
| Tool loading | MCP server subprocess (`mcp_server.py`) |
| Grading | JSONPath checks + transcript rules + optional hash |
| `harness_adapter` in `run.yaml` | Not needed (auto-selected) |

---

## Task Directory Layout

```
tasks/<category>/<task_id>/
├── task.yaml                    # required — task config
├── grading.yaml                 # required — grading config
├── system_prompt.md             # optional — agent system prompt / wiki
├── initial_state.json           # optional — seed data for JSON DB
├── mcp_server.py                # required if agent uses tools
├── tools/                       # optional — tool implementation modules
│   └── *.py
└── fixtures/
    └── tools.json               # optional — pre-generated tool schemas (cache)
```

The `fixtures/tools.json` file is auto-generated on the first run if it does
not exist (see [Tool Schemas](#tool-schemas-fixturestoolsjson)).

---

## task.yaml Schema

```yaml
task_id: "shop_orders_02"                # required — globally unique identifier
name: "Online Store — Place an Order"    # required — human-readable name
category: "tool_use"                     # required — terminal | browser | mobile | tool_use | …
description: >                           # optional — long description
  Use store tools to browse the catalog …

max_turns: 14                            # optional — overrides orchestrator default

initial_user_message: >                  # optional — first message from user to agent
  Hi! I'd like to buy 1 × Wireless Headphones …

initial_state:
  json_db: "initial_state.json"          # path relative to task dir (or inline dict)
  filesystem:                            # optional — files copied into /env/fs/agent-visible/
    copy:
      - from: "fixtures/file.json"
        to: "/env/fs/agent-visible/file.json"
  initialization_actions:               # optional — tool calls run before trial starts
    - env_type: agent
      tool_name: seed_db
      arguments: {}

tools:
  agent:
    mcp_server: "mcp_server.py"          # path to MCP server script (relative to task dir)
    enabled:                             # tool names exposed to the agent
      - list_products
      - get_customer
      - place_order
      - confirm_payment
  user:
    enabled: []                          # tool names available to user simulator

user_simulator:
  mode: llm                             # llm | scripted
  persona: cooperative                  # short persona label for the LLM
  backstory: >                          # scenario description (revealed gradually to agent)
    You are customer Alex Torres (C-101) …
    When the assistant confirms, end with ###STOP###.

policies:
  guidance:                             # optional — guidelines shown in system prompt
    - "Always call list_products before placing an order."

grading: "grading.yaml"                 # path to grading file (relative to task dir)
system_prompt: "system_prompt.md"       # path to system prompt file (relative to task dir)
```

### initial_state.json format

A flat JSON object where each key is a collection name and each value is either
a list of records or a dict of records keyed by ID:

```json
{
  "products": [
    {"id": "P-001", "name": "Wireless Headphones", "price": 89.99, "stock": 42},
    {"id": "P-002", "name": "USB-C Hub 7-Port",    "price": 34.50, "stock": 15}
  ],
  "customers": [
    {"id": "C-101", "name": "Alex Torres", "balance": 300.00}
  ],
  "orders": []
}
```

---

## grading.yaml Schema

```yaml
combine:
  method: weighted                       # weighted | min | all
  weights:
    state_checks: 0.70
    transcript_rules: 0.30
  pass_threshold: 0.75                   # score ≥ threshold → trial passes

state_checks:
  hash:
    enabled: true
    weight: 0.60                         # blend: hash×0.6 + jsonpaths×0.4
    golden_actions:                      # canonical tool sequence to replay
      - name: place_order
        kwargs:
          customer_id: "C-101"
          items:
            - {product_id: "P-001", quantity: 1, unit_price: 89.99}

  jsonpaths:                             # individual field assertions
    - path: "$.db.orders[0].status"
      equals: "paid"
      description: "Order status is 'paid' after confirm_payment"

    - path: "$.db.customers[0].balance"
      equals: 141.01
      description: "Balance deducted correctly"

transcript_rules:
  max_turns: 14

  disallow_regex:
    - "(?i)(unable to place|order failed)"

  required_actions:                      # tool calls that must appear in transcript
    - action_id: "browse_catalog"
      requestor: assistant
      name: list_products
      arguments: {}
      compare_args: []                   # empty = only check name, ignore args

  communicate_info:                      # strings the agent must say to the user
    - info: "O-001"
      required: true
    - info: "paid"
      required: true
```

### Grading scoring

| Component | Description |
|-----------|-------------|
| `state_checks` | DB state after the episode. Blends hash score and JSONPath score using `hash.weight`. |
| `hash` | Replays `golden_actions` on a fresh DB copy, hashes the result, compares to agent's final state. All-or-nothing. |
| `jsonpaths` | Per-assertion checks against the agent's final DB state. Partial credit. |
| `transcript_rules` | Checks tool call sequence and agent messages in the conversation transcript. |
| `combine` | Weighted blend of component scores. Pass when result ≥ `pass_threshold`. |

---

## Tool Schemas (fixtures/tools.json)

The adapter needs parameter schemas to tell the LLM what arguments each tool
accepts.  Resolution order:

1. **`fixtures/tools.json`** — pre-generated static file (fastest, avoids
   subprocess overhead on every run).  Preferred.
2. **Live MCP query** (`tools/list`) — used when `fixtures/tools.json` does
   not exist.  The adapter starts `mcp_server.py` as a subprocess, performs
   the MCP handshake, calls `tools/list`, and writes the result back to
   `fixtures/tools.json` for subsequent runs.

`fixtures/tools.json` format (list of tool descriptors):

```json
[
  {
    "name": "place_order",
    "description": "Place a new customer order.",
    "parameters": {
      "type": "object",
      "properties": {
        "customer_id": {"type": "string"},
        "items": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "product_id": {"type": "string"},
              "quantity":   {"type": "integer"},
              "unit_price": {"type": "number"}
            },
            "required": ["product_id", "quantity", "unit_price"]
          }
        }
      },
      "required": ["customer_id", "items"]
    }
  }
]
```

> **Note:** If `fixtures/tools.json` is absent and the live MCP query fails,
> the adapter falls back to empty schemas (`{"type": "object", "properties": {}}`),
> which causes the LLM to call tools with missing arguments.  Always commit
> `fixtures/tools.json` or ensure `mcp_server.py` is importable before running.

---

## How the Adapter Works

### Task discovery

`_discover_tasks()` is called lazily on first access.  It expands the
`tasks_glob` pattern (optionally prefixed with each `task_packs` root) and
reads every matching `task.yaml`.  Duplicate `task_id` values use first-wins
(the second occurrence is silently skipped).

### `create_environment(task_id)`

1. Loads `task.yaml` → `TaskConfig`.
2. Reads `initial_state.json` (or inline dict) into `data: dict`.
3. Reads `system_prompt` file into `wiki: str`.
4. Returns `AdapterEnvironment(data=data, tools=[], wiki=wiki, rules=[], task_dir=…)`.

Tools are **not** loaded here — they are provided by the MCP server subprocess
at runtime (see `get_registry_tools` note below).

### `get_tools` / `get_registry_tools`

Both return empty lists.  For native tasks the orchestrator loads the MCP
server directly via `InvocationStyle.MCP_SERVER` and the tool registry is
populated at trial start time, not at adapter init time.

### `get_grading_config(task_id)`

Reads `grading.yaml` and returns a `GradingConfig` object.

### `compute_golden_hash(task_id, env)`

Returns the pre-computed `hash.expected_state_hash` from `grading.yaml` if
`hash.enabled = true` and no `golden_actions` are listed.  Returns `None`
otherwise (hash is computed dynamically by the grading engine via
`golden_actions` replay).

### `reset_environment(env)`

No-op.  State reset between trials is handled by the orchestrator (JSON DB
service is re-initialized per trial).

---

## Docker Execution

When the orchestrator runs with a Docker runtime, `to_task_description()` is
called to serialize the task for transfer to the Runner container.

### What gets serialized

| Field | Source |
|-------|--------|
| `agent_tools` | `tools.agent.enabled` list + schemas from `fixtures/tools.json` |
| `user_tools` | `tools.user.enabled` list |
| `initial_state.tables` | `initial_state.json` (collection → list of records) |
| `initialization_actions` | `initial_state.initialization_actions` |
| `grading` | `grading.yaml` (state checks + transcript rules) |
| `system_prompt` | system prompt file content |
| `tool_artifacts` | All `.py` files (recursive) + top-level `.json`, `.yaml`, `.yml`, `.md`, `.txt` files in the task dir, base64-encoded |
| `metadata.mcp_server_ref` | Relative path to `mcp_server.py` |

### Artifact bundling

`_bundle_task_artifacts(task_dir)` encodes source files as base64 strings
keyed by relative path (e.g. `"mcp_server.py"`, `"tools/orders.py"`).  The
Runner extracts these into a temporary directory and launches `mcp_server.py`
as a subprocess, reconstructing the original layout without any host
filesystem access.

**Bundling scope:**
- Python (`.py`) files — bundled **recursively** (`**/*.py`), so all subdirectory modules (e.g. `tools/*.py`) are included.
- Data files (`.json`, `.yaml`, `.yml`, `.md`, `.txt`) — bundled from the **task root only** (not recursive).  `fixtures/tools.json` is *not* bundled as an artifact — its contents are resolved on the host and embedded directly into each `ToolSchema.parameters` inside `TaskDescription`.

### Tool schema resolution for Docker

Same two-step logic as local runs: `fixtures/tools.json` first, then live MCP
query.  The resolved schemas are embedded in `agent_tools[*].parameters` inside
`TaskDescription` and sent to the Runner via gRPC `RegisterTrial`.

---

## Run Configuration

### Default (no harness_adapter needed)

```yaml
models:
  agent:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.0
  user:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.7

evaluation:
  tasks_glob: "tasks/tool_use/shop_orders_02/task.yaml"
  output_dir: "output/shop_orders_02"

orchestrator:
  repeats: 3
  max_turns: 14
```

### Multiple task packs

```yaml
evaluation:
  task_packs:
    - "/abs/path/private-pack"
    - "."
  tasks_glob: "**/task.yaml"
  output_dir: "output/combined"
```

When `task_packs` is set, `tasks_glob` must be a relative pattern.  It is
expanded under each pack root in order; first match wins on duplicate `task_id`.

---

## Known Issues

### No issues found in production evaluation runs

The `native` adapter was not exercised in the frozen retail evaluation runs
(see `docs/ADAPTERS.md`).  No field issues have been logged.

### Design limitations

#### Tools not encapsulated in adapter

`get_registry_tools()` returns `[]`.  Tool loading is handled by the
orchestrator directly via the MCP server path from `task.yaml`.  This is a
known architectural gap — future work would move MCP server loading fully into
the adapter (noted in the source at `native.py:185`).

#### `compute_golden_hash` delegates to grading engine

When `grading.yaml` contains `golden_actions`, `compute_golden_hash()` returns
`None` and leaves hash computation to the `GradingEngine`.  The method does
not execute golden actions itself because that requires MCP server access that
is not available at adapter init time.

#### User tools not supported

`user_tools` in `to_task_description()` is always an empty list because
`user.enabled: []` in all current native tasks.  If user tools are added in
future tasks, the schema-loading logic will need to be implemented (user tools
use `InvocationStyle.TAU_SYNC`, not MCP, so schemas cannot come from
`fixtures/tools.json`).
