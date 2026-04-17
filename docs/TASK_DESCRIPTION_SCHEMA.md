# Serializable Task Description Schema

The universal contract between the **Loader** (host) and **Runtime** (runner container).
The loader reads adapter-specific formats and produces this. The runner only understands this.

## Architecture Context

```
Host (Loader)                    Runner Container (Runtime)         DB Service Container
  Reads source files               Receives TaskDescription           State storage
  Resolves paths, loads content    Reconstructs tools from ToolSource  Schema + unstable fields
  Extracts unstable field metadata Initializes state via DB Service    Stable state filtering
  Produces TaskDescription JSON    Executes tools, grades results      Snapshot/restore
```

## Design Principles

1. **Pure Data** — no Python callables, class references, or live objects
2. **Self-Contained** — all information needed to run and grade a task
3. **Adapter-Agnostic** — single schema supports Native, Tau, and TlkMcpCore
4. **Unstable Fields as Data** — field stability metadata is explicit, not Python annotations
5. **Reconstructable** — `ToolSource` provides enough info to reconstruct tools at runtime
6. **Canonical Hash Algorithm** — all components use identical JSON-based SHA-256 hashing

---

## Schema

```python
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# =============================================================================
# Enums
# =============================================================================

class AdapterType(str, Enum):
    """Source adapter that produced this description."""
    NATIVE = "native"
    TAU = "tau"
    TLK_MCP_CORE = "tlk_mcp_core"


class InvocationStyle(str, Enum):
    """How the runtime invokes this tool."""
    TAU_SYNC = "tau_sync"          # Tau: Tool.invoke(data, **kwargs)
    MCP_ASYNC = "mcp_async"        # TlkMcpCore: asyncio.run(tool.run(db, kwargs))
    MCP_SERVER = "mcp_server"      # Native: MCP server subprocess


# =============================================================================
# Tool Definitions
# =============================================================================

class ToolSource(BaseModel):
    """
    Information needed to reconstruct tool execution at runtime.
    
    The runtime uses this to locate and instantiate the actual tool
    implementation in the container. Tool code must be pre-installed
    or mounted in the container.
    """
    toolset: str                                  # Package/directory: "zendesk", "airline", "telecom"
    module_path: str                              # Module within toolset: "tools.create_item"
    class_name: str                               # Class/function: "CreateItem", "BookReservation"
    invocation_style: InvocationStyle = InvocationStyle.TAU_SYNC
    
    # For MCP server tools only
    mcp_server_script: Optional[str] = None       # Relative path: "mcp_server.py"


class ToolSchema(BaseModel):
    """Complete tool definition with schema and source for reconstruction."""
    name: str
    description: str
    parameters: Dict[str, Any]                    # JSON Schema format (OpenAI function calling)
    
    # Metadata
    category: Literal["read", "write", "compute"] = "compute"
    timeout_s: float = 30.0

    # How to reconstruct this tool at runtime
    source: ToolSource


# =============================================================================
# State and Data
# =============================================================================

class UnstableFieldSpec(BaseModel):
    """
    A field excluded from grading hash comparison.
    
    These are fields with non-deterministic values: auto-generated IDs,
    timestamps, or LLM-generated content. The DB service uses this to
    filter them out when computing stable state.
    
    IMPORTANT: All adapters MUST provide unstable field specs:
    - TlkMcpCore: Extracted from UnstableField annotations in Pydantic models
    - Tau: Loaded from unstable_fields.yaml in environment directory
    - Native: Defined in task.yaml or grading.yaml
    """
    table_name: str                               # "zendesk_tickets", "reservations"
    field_name: str                               # "id", "created_at", "subject"
    reason: Literal["auto_id", "timestamp", "llm_generated", "random"] = "auto_id"


class TableSchema(BaseModel):
    """Schema for a database table. Used by DB Service for validation."""
    table_name: str
    fields: Dict[str, str]                        # field_name → type ("string", "integer", "datetime")
    primary_key: str = "id"


class InitialStateConfig(BaseModel):
    """
    Complete initial state specification.
    
    Contains all data and metadata needed to initialize the DB service.
    """
    # Data: table_name → list of records
    tables: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    
    # Schema: table definitions for validation
    schemas: List[TableSchema] = Field(default_factory=list)
    
    # Unstable fields: single source of truth for hash exclusion
    unstable_fields: List[UnstableFieldSpec] = Field(default_factory=list)


# =============================================================================
# Pre-Trial Actions
# =============================================================================

class InitializationAction(BaseModel):
    """
    Action to execute before trial starts.
    
    Used by Native adapter for user device setup (toggle_airplane_mode, etc.)
    """
    env_type: Literal["assistant", "user"]
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# User Simulator
# =============================================================================

class UserSimulatorConfig(BaseModel):
    """Configuration for the user simulator."""
    mode: Literal["scripted", "llm"] = "llm"
    persona: str = "cooperative"
    backstory: str = ""                           # User instruction/context
    
    # First message to start conversation (TlkMcpCore)
    first_message: Optional[str] = None
    
    # User context data injected into conversation (TlkMcpCore)
    user_context: Optional[Dict[str, Any]] = None
    
    # For scripted mode
    scripted_flow: Optional[List[Dict[str, str]]] = None


# =============================================================================
# Search / TypeSense
# =============================================================================

class SearchConfig(BaseModel):
    """Configuration for knowledge base search (TypeSense)."""
    enabled: bool = False
    domain_name: Optional[str] = None             # "external_retail_v3"
    documents_path: Optional[str] = None          # Path to docindex/ directory


# =============================================================================
# Grading
# =============================================================================

class GoldenAction(BaseModel):
    """
    A tool call in the expected sequence.
    
    Execute these on fresh state to compute the expected final state hash.
    """
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class EnvAssertion(BaseModel):
    """
    Assertion on environment state after trial.
    
    Used by Native adapter for checking device state.
    """
    env_type: Literal["assistant", "user"]
    func_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    assert_value: Any = True
    message: Optional[str] = None


class RequiredAction(BaseModel):
    """Tool call that must appear in the trajectory."""
    action_id: str
    requestor: Literal["assistant", "user"]
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    compare_args: Optional[List[str]] = None      # Which args to compare, None = all


class StateChecksConfig(BaseModel):
    """State-based grading configuration."""
    # Hash comparison
    hash_enabled: bool = False
    expected_hash: Optional[str] = None           # Pre-computed (if available)
    golden_actions: List[GoldenAction] = Field(default_factory=list)
    
    # JSONPath assertions
    jsonpath_checks: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Environment assertions (Native adapter)
    env_assertions: List[EnvAssertion] = Field(default_factory=list)


class TranscriptRulesConfig(BaseModel):
    """Transcript-based grading configuration."""
    must_contain: List[str] = Field(default_factory=list)
    disallow_regex: List[str] = Field(default_factory=list)
    max_turns: Optional[int] = None
    required_actions: List[RequiredAction] = Field(default_factory=list)
    communicate_info: List[Dict[str, Any]] = Field(default_factory=list)


class LLMJudgeConfig(BaseModel):
    """LLM-based grading configuration."""
    model_ref: str                                # "openrouter/anthropic/claude-sonnet-4.5"
    rubric: str                                   # Grading rubric text
    output_schema: Dict[str, Any]                 # Expected output format


class GradingConfig(BaseModel):
    """
    Complete grading configuration.
    
    Supports multiple methods combined with weights.
    """
    combine_method: Literal["weighted", "all_pass", "any_pass"] = "weighted"
    weights: Dict[str, float] = Field(default_factory=lambda: {"state_checks": 1.0})
    pass_threshold: float = 0.8
    
    state_checks: Optional[StateChecksConfig] = None
    transcript_rules: Optional[TranscriptRulesConfig] = None
    llm_judge: Optional[LLMJudgeConfig] = None


# =============================================================================
# Main TaskDescription
# =============================================================================

class TaskDescription(BaseModel):
    """
    Complete serializable task description.
    
    Produced by the Loader (host) from adapter-specific formats.
    Consumed by the Runtime (runner container) for execution and grading.
    """
    
    # --- Identity ---
    task_id: str
    name: str
    category: str                                 # Domain: "airline", "telecom", "retail"
    description: str                              # Task description / user goal
    adapter_type: AdapterType
    schema_version: str = "1.0.0"
    
    # --- System Prompt ---
    system_prompt: str                            # Full content, not file path
    
    # --- Tools ---
    agent_tools: List[ToolSchema] = Field(default_factory=list)
    user_tools: List[ToolSchema] = Field(default_factory=list)  # User-side device tools
    
    # --- State ---
    initial_state: InitialStateConfig = Field(default_factory=InitialStateConfig)
    initialization_actions: List[InitializationAction] = Field(default_factory=list)
    
    # --- User Simulator ---
    user_simulator: UserSimulatorConfig = Field(default_factory=UserSimulatorConfig)
    
    # --- Search ---
    search: SearchConfig = Field(default_factory=SearchConfig)
    
    # --- Grading ---
    grading: GradingConfig = Field(default_factory=GradingConfig)
    
    # --- Metadata ---
    source_files: Dict[str, str] = Field(default_factory=dict)  # For debugging
    generated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)      # Adapter-specific extras

    model_config = {"extra": "forbid"}
```

---

## Examples

### TlkMcpCore

```json
{
  "task_id": "ST006-001",
  "name": "ST006-001",
  "category": "external_retail_v3",
  "description": "Customer wants to return a DSLR camera for a refund",
  "adapter_type": "tlk_mcp_core",
  "schema_version": "1.0.0",

  "system_prompt": "# External Retail Customer Service\n\nYou are a customer service agent...",

  "agent_tools": [
    {
      "name": "zendesk_create_item",
      "description": "Create a new item in Zendesk",
      "parameters": {
        "type": "object",
        "properties": {
          "table": {"type": "string", "enum": ["tickets", "users"]},
          "item": {"type": "object"}
        },
        "required": ["table", "item"]
      },
      "source": {
        "toolset": "sandbox_auto_insurance_zendesk",
        "module_path": "tools.create_item",
        "class_name": "CreateItem",
        "invocation_style": "mcp_async"
      }
    },
    {
      "name": "create_rma",
      "description": "Create a Return Merchandise Authorization",
      "parameters": {
        "type": "object",
        "properties": {
          "order_id": {"type": "string"},
          "customer_id": {"type": "string"},
          "return_reason": {"type": "string"}
        },
        "required": ["order_id", "customer_id", "return_reason"]
      },
      "source": {
        "toolset": "external_retail_toolset_loop_returns",
        "module_path": "tools.create_rma",
        "class_name": "CreateRMA",
        "invocation_style": "mcp_async"
      }
    }
  ],

  "user_tools": [],

  "initial_state": {
    "tables": {
      "zendesk_users": [
        {"id": "10002", "name": "Jane Roe", "email": "customer2@example.com"}
      ],
      "orders": [
        {"id": "ORD-60010", "customer_id": "CUS-20010", "status": "delivered", "total_amount": 799}
      ],
      "order_line_items": [
        {"id": "LI-60010-001", "order_id": "ORD-60010", "product_name": "DSLR camera", "final_price": 799}
      ]
    },
    "schemas": [
      {
        "table_name": "zendesk_tickets",
        "fields": {"id": "string", "subject": "string", "status": "string", "created_at": "datetime"},
        "primary_key": "id"
      },
      {
        "table_name": "rma_records",
        "fields": {"id": "string", "order_id": "string", "created_at": "datetime"},
        "primary_key": "id"
      }
    ],
    "unstable_fields": [
      {"table_name": "zendesk_tickets", "field_name": "id", "reason": "auto_id"},
      {"table_name": "zendesk_tickets", "field_name": "subject", "reason": "llm_generated"},
      {"table_name": "zendesk_tickets", "field_name": "created_at", "reason": "timestamp"},
      {"table_name": "rma_records", "field_name": "id", "reason": "auto_id"},
      {"table_name": "rma_records", "field_name": "created_at", "reason": "timestamp"}
    ]
  },

  "user_simulator": {
    "mode": "llm",
    "persona": "customer",
    "backstory": "You are Jane Roe contacting customer service about order ORD-60010.",
    "first_message": "I bought a DSLR camera about a month ago and it was delivered. I want to return it for a refund.",
    "user_context": {"name": "Jane Roe", "email": "customer2@example.com", "order_id": "ORD-60010"}
  },

  "search": {
    "enabled": true,
    "domain_name": "external_retail_v3",
    "documents_path": "tasks/tlk_mcp_core/external_retail_server_v3/src/domains/external_retail_v3/docindex"
  },

  "grading": {
    "combine_method": "weighted",
    "weights": {"state_checks": 1.0},
    "pass_threshold": 1.0,
    "state_checks": {
      "hash_enabled": true,
      "golden_actions": [
        {"tool_name": "zendesk_create_item", "arguments": {"table": "tickets", "item": {"status": "open", "subject": "Return request", "requester_id": "10002"}}},
        {"tool_name": "create_rma", "arguments": {"order_id": "ORD-60010", "customer_id": "CUS-20010", "return_reason": "changed_mind"}}
      ]
    }
  },

  "source_files": {
    "testcase": "tasks/tlk_mcp_core/.../testcases/ST006-001.json",
    "instruction": "tasks/tlk_mcp_core/.../instruction.md",
    "tools_library": "tasks/tlk_mcp_core/mcp-tools-library"
  },
  "metadata": {
    "domain": "external_retail_v3"
  }
}
```

### Tau

```json
{
  "task_id": "airline_task_001",
  "name": "Airline Task 1",
  "category": "airline",
  "description": "Book a flight from New York to Seattle",
  "adapter_type": "tau",
  "schema_version": "1.0.0",

  "system_prompt": "# Airline Booking System\n\nYou are a customer service agent...",

  "agent_tools": [
    {
      "name": "book_reservation",
      "description": "Book a new flight reservation",
      "parameters": {
        "type": "object",
        "properties": {
          "user_id": {"type": "string"},
          "origin": {"type": "string"},
          "destination": {"type": "string"}
        },
        "required": ["user_id", "origin", "destination"]
      },
      "source": {
        "toolset": "airline",
        "module_path": "tau_tools.book_reservation",
        "class_name": "BookReservation",
        "invocation_style": "tau_sync"
      }
    }
  ],

  "user_tools": [],

  "initial_state": {
    "tables": {
      "users": [{"user_id": "mia_li_3668", "name": "Mia Li"}],
      "flights": [{"flight_number": "HAT136", "origin": "JFK", "destination": "SEA"}],
      "reservations": []
    },
    "schemas": [],
    "unstable_fields": []
  },

  "user_simulator": {
    "mode": "llm",
    "persona": "customer",
    "backstory": "Your user id is mia_li_3668. You want to fly from New York to Seattle."
  },

  "search": {"enabled": false},

  "grading": {
    "combine_method": "weighted",
    "weights": {"state_checks": 1.0},
    "pass_threshold": 1.0,
    "state_checks": {
      "hash_enabled": true,
      "golden_actions": [
        {"tool_name": "book_reservation", "arguments": {"user_id": "mia_li_3668", "origin": "JFK", "destination": "SEA"}}
      ]
    }
  },

  "source_files": {
    "tasks": "tasks/airline/tasks_test.py",
    "tools": "tasks/airline/tools/__init__.py"
  }
}
```

### Native

```json
{
  "task_id": "telecom_task_002",
  "name": "Mobile Data / Slow Internet Issues",
  "category": "telecom",
  "description": "User experiencing mobile data issues",
  "adapter_type": "native",
  "schema_version": "1.0.0",

  "system_prompt": "# Telecom Support Manual\n\nYou are a customer service agent...",

  "agent_tools": [
    {
      "name": "get_customer_by_phone",
      "description": "Look up customer by phone number",
      "parameters": {
        "type": "object",
        "properties": {"phone_number": {"type": "string"}},
        "required": ["phone_number"]
      },
      "source": {
        "toolset": "telecom",
        "module_path": "mcp_server",
        "class_name": "get_customer_by_phone",
        "invocation_style": "mcp_server",
        "mcp_server_script": "mcp_server.py"
      }
    }
  ],

  "user_tools": [
    {
      "name": "toggle_airplane_mode",
      "description": "Toggle airplane mode on/off",
      "parameters": {"type": "object", "properties": {}},
      "source": {
        "toolset": "telecom_user",
        "module_path": "user_device",
        "class_name": "toggle_airplane_mode",
        "invocation_style": "tau_sync"
      }
    }
  ],

  "initial_state": {
    "tables": {
      "customers": [{"id": "CUST-001", "name": "John Smith", "phone": "555-123-2002"}],
      "data_plans": [{"customer_id": "CUST-001", "data_remaining_gb": 0.5}]
    },
    "schemas": [
      {"table_name": "customers", "fields": {"id": "string", "name": "string", "phone": "string"}, "primary_key": "id"}
    ],
    "unstable_fields": []
  },

  "initialization_actions": [
    {"env_type": "user", "tool_name": "set_user_info", "arguments": {"name": "John Smith", "phone_number": "555-123-2002"}},
    {"env_type": "user", "tool_name": "turn_airplane_mode_on", "arguments": {}},
    {"env_type": "user", "tool_name": "turn_data_off", "arguments": {}}
  ],

  "user_simulator": {
    "mode": "llm",
    "persona": "cooperative",
    "backstory": "You are John Smith with phone number 555-123-2002..."
  },

  "search": {"enabled": false},

  "grading": {
    "combine_method": "weighted",
    "weights": {"state_checks": 0.67, "transcript_rules": 0.33},
    "pass_threshold": 1.0,
    "state_checks": {
      "env_assertions": [
        {"env_type": "user", "func_name": "assert_mobile_data_status", "arguments": {"expected_status": true}},
        {"env_type": "user", "func_name": "assert_internet_speed", "arguments": {"expected_speed": 200}}
      ]
    },
    "transcript_rules": {
      "max_turns": 20,
      "required_actions": [
        {"action_id": "toggle_airplane_mode_0", "requestor": "user", "tool_name": "toggle_airplane_mode"},
        {"action_id": "toggle_data_1", "requestor": "user", "tool_name": "toggle_data"}
      ]
    }
  },

  "source_files": {
    "task": "tasks/telecom/task_002/task.yaml",
    "grading": "tasks/telecom/task_002/grading.yaml"
  }
}
```

---

## Tool Reconstruction at Runtime

The runtime uses `ToolSource` to reconstruct tools:

```python
def reconstruct_tool(tool: ToolSchema, db_client: DBServiceClient) -> callable:
    source = tool.source

    if source.invocation_style == "tau_sync":
        module = importlib.import_module(f"{source.toolset}.{source.module_path}")
        tool_class = getattr(module, source.class_name)
        return TauToolWrapper(tool.name, tool_class, db_client)

    elif source.invocation_style == "mcp_async":
        module = importlib.import_module(f"mcp_tools_library.{source.toolset}.{source.module_path}")
        tool_class = getattr(module, source.class_name)
        return MCPAsyncToolWrapper(tool.name, tool_class, db_client)

    elif source.invocation_style == "mcp_server":
        return MCPServerToolProxy(tool.name, source.mcp_server_script)
```

**Requirement:** tool packages must be pre-installed or mounted in the runner container.

---

## DB Service API

```
POST   /init                  ← initialize with initial state + schemas + unstable fields
GET    /state                 ← full current state
PATCH  /state/{table}         ← mutations (insert, update, delete)
GET    /state/stable          ← state with unstable fields filtered out
GET    /state/hash            ← SHA256 of stable state
POST   /snapshot              ← create named snapshot of current state
POST   /restore/{name}        ← restore from snapshot (for golden path execution)
```

---

## Canonical Hash Algorithm

**CRITICAL:** All components MUST use the identical hash algorithm for grading to work correctly.

```python
import hashlib
import json
from typing import Any, Dict

def compute_stable_hash(state: Dict[str, Any]) -> str:
    """
    Compute canonical hash of stable state.
    
    This algorithm MUST be used by:
    - DB Service /state/hash endpoint
    - TlkMcpCore adapter grading
    - Tau adapter grading
    - Any other component computing state hashes
    
    Algorithm:
    1. JSON serialize with sort_keys=True, separators=(",", ":")
    2. UTF-8 encode
    3. SHA-256 hexdigest
    
    IMPORTANT: Do NOT use str(tuple) or other serialization methods!
    """
    json_str = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()
```

**Key Requirements:**
- `sort_keys=True` — deterministic key ordering
- `separators=(",", ":")` — compact JSON, no spaces (NOT default `(", ", ": ")`)
- `encode("utf-8")` — explicit UTF-8 encoding
- `default=str` — handle datetime and other non-JSON types

**Reference Implementation:** See `tolokaforge/core/hash.py` and `tolokaforge/core/grading/state_checks.py`.

---

## What Each Loader Produces

| Adapter | Reads | Key Transformations |
|---------|-------|-------------------|
| FrozenMcpCore | Bundled `_domain/` directory, converted task data | Self-contained tasks with tool artifacts, stable hash grading. |
| Native | task.yaml, grading.yaml, system_prompt.md, initial_state.json | Queries MCP server for tool schemas → `ToolSource` with `mcp_server`. Copies grading.yaml content → `GradingConfig`. |
