# DB Service API Specification

The DB Service provides schema-aware JSON state storage with unstable field filtering
for hash-based grading. It extends the existing json-db service from PR #22 to support
the Docker architecture's trial isolation and grading requirements.

## Architecture Context

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            RUNNER CONTAINER                                  │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐   │
│  │ Adapter Runtime │  │ Tool Execution  │  │ Grading Engine              │   │
│  │ - Tool Reconstr │  │ - MCP/Tau/Native│  │ - Golden Path Execution     │   │
│  │ - Schema Gen    │  │ - State Mutation│  │ - Hash Comparison           │   │
│  └────────┬────────┘  └────────┬────────┘  └──────────────┬──────────────┘   │
│           │                    │                          │                   │
│           └────────────────────┴──────────────────────────┘                   │
│                                   │ HTTP                                      │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────┴────────────────────────────────────────────┐
│                          DB SERVICE CONTAINER                                  │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐    │
│  │ State Storage   │  │ Schema Registry │  │ Stable State Engine         │    │
│  │ - Per-trial     │  │ - TableSchema   │  │ - Unstable field filtering  │    │
│  │ - Snapshots     │  │ - UnstableField │  │ - Hash computation          │    │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Design Principles

1. **Trial Isolation** — Each trial has isolated state, schemas, and snapshots
2. **Schema-Aware** — Stores table schemas for validation and type inference
3. **Unstable Field Filtering** — Explicit field exclusion for deterministic hashing
4. **Tau/TlkMcpCore Compatible** — Hash algorithm matches existing implementations
5. **Snapshot/Restore** — Supports golden path execution during grading

---

## HTTP API Specification

### Base URL

```
http://db-service:8000
```

All endpoints accept and return JSON. Trial isolation is achieved via `trial_id` path parameter.

---

### 1. Initialize Trial

**`POST /trials/{trial_id}/init`**

Initialize a trial with initial state, schemas, and unstable field specifications.
This is the primary entry point called by the Runner after receiving `RegisterTrial`.

#### Request Body

```json
{
  "tables": {
    "users": [
      {"user_id": "mia_li_3668", "name": "Mia Li", "email": "mia@example.com"}
    ],
    "flights": [
      {"flight_number": "HAT136", "origin": "JFK", "destination": "SEA", "price": 450}
    ],
    "reservations": []
  },
  "schemas": [
    {
      "table_name": "reservations",
      "fields": {
        "id": "string",
        "user_id": "string",
        "flight_number": "string",
        "created_at": "datetime",
        "status": "string"
      },
      "primary_key": "id"
    }
  ],
  "unstable_fields": [
    {"table_name": "reservations", "field_name": "id", "reason": "auto_id"},
    {"table_name": "reservations", "field_name": "created_at", "reason": "timestamp"}
  ]
}
```

#### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tables` | `Dict[str, List[Dict]]` | Yes | Initial data: table_name → list of records |
| `schemas` | `List[TableSchema]` | No | Table schema definitions for validation |
| `unstable_fields` | `List[UnstableFieldSpec]` | No | Fields to exclude from stable hash |

**TableSchema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `table_name` | `string` | Yes | Table identifier |
| `fields` | `Dict[str, string]` | Yes | field_name → type ("string", "integer", "float", "boolean", "datetime") |
| `primary_key` | `string` | No | Primary key field (default: "id") |

**UnstableFieldSpec:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `table_name` | `string` | Yes | Table containing the unstable field |
| `field_name` | `string` | Yes | Field name to exclude from hash |
| `reason` | `string` | No | Reason: "auto_id", "timestamp", "llm_generated", "random" |

#### Response

```json
{
  "status": "ok",
  "trial_id": "airline_task_001:0",
  "tables_initialized": ["users", "flights", "reservations"],
  "schemas_registered": 1,
  "unstable_fields_registered": 2,
  "initial_hash": "a1b2c3d4e5f6..."
}
```

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Invalid request body |
| 409 | Trial already exists (use reset or delete first) |

---

### 2. Get Full State

**`GET /trials/{trial_id}/state`**

Get the complete current state including all fields.

#### Response

```json
{
  "data": {
    "users": [
      {"user_id": "mia_li_3668", "name": "Mia Li", "email": "mia@example.com"}
    ],
    "reservations": [
      {"id": "RES-001", "user_id": "mia_li_3668", "flight_number": "HAT136", "created_at": "2024-01-15T10:00:00Z", "status": "confirmed"}
    ]
  },
  "version": 3,
  "full_hash": "abc123...",
  "stable_hash": "def456..."
}
```

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tables` | `string` | all | Comma-separated list of tables to return |

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 404 | Trial not found |

---

### 3. Get Stable State

**`GET /trials/{trial_id}/state/stable`**

Get state with unstable fields filtered out. Used for grading comparison.

#### Response

```json
{
  "data": {
    "users": [
      {"user_id": "mia_li_3668", "name": "Mia Li", "email": "mia@example.com"}
    ],
    "reservations": [
      {"user_id": "mia_li_3668", "flight_number": "HAT136", "status": "confirmed"}
    ]
  },
  "version": 3,
  "stable_hash": "def456...",
  "filtered_fields": [
    {"table": "reservations", "field": "id"},
    {"table": "reservations", "field": "created_at"}
  ]
}
```

Note: The `reservations` records have `id` and `created_at` removed because they are registered as unstable fields.

---

### 4. Get Stable Hash

**`GET /trials/{trial_id}/state/hash`**

Get SHA-256 hash of the stable state. This is the primary endpoint for grading comparison.

#### Response

```json
{
  "stable_hash": "def456789abc...",
  "full_hash": "abc123456def...",
  "version": 3
}
```

#### Hash Computation Algorithm

The hash is computed to match the existing Tau/TlkMcpCore implementation:

```python
def compute_stable_hash(state: Dict, unstable_fields: List[UnstableFieldSpec]) -> str:
    # 1. Deep copy state
    stable_state = copy.deepcopy(state)
    
    # 2. Remove unstable fields from each table
    for spec in unstable_fields:
        table = stable_state.get(spec.table_name, [])
        for record in table:
            if spec.field_name in record:
                del record[spec.field_name]
    
    # 3. Convert datetime objects to ISO strings
    stable_state = convert_datetime_to_str(stable_state)
    
    # 4. Serialize with sorted keys and compact separators
    json_str = json.dumps(stable_state, sort_keys=True, separators=(",", ":"))
    
    # 5. Compute SHA-256
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()
```

**CRITICAL:** The hash algorithm must match the canonical algorithm defined in [`TASK_DESCRIPTION_SCHEMA.md`](TASK_DESCRIPTION_SCHEMA.md#canonical-hash-algorithm):
- `sort_keys=True` for deterministic key ordering
- `separators=(",", ":")` for compact JSON (no spaces) — NOT default `(", ", ": ")`
- `encode("utf-8")` — explicit UTF-8 encoding
- SHA-256 hexdigest

**WARNING:** Using different separators or serialization methods (e.g., `str(tuple)`) will cause hash mismatches and grading failures.

---

### 5. Mutate State

**`PATCH /trials/{trial_id}/state/{table_name}`**

Apply mutations to a specific table. Used by tools to modify state.

#### Request Body

```json
{
  "operations": [
    {
      "op": "insert",
      "record": {"id": "RES-001", "user_id": "mia_li_3668", "flight_number": "HAT136", "status": "confirmed"}
    }
  ],
  "etag": "optional-for-optimistic-locking"
}
```

#### Operation Types

**Insert:**
```json
{"op": "insert", "record": {"id": "...", "field": "value"}}
```

**Update:**
```json
{"op": "update", "filter": {"id": "RES-001"}, "set": {"status": "cancelled"}}
```

**Delete:**
```json
{"op": "delete", "filter": {"id": "RES-001"}}
```

**Upsert:**
```json
{"op": "upsert", "record": {"id": "RES-001", "status": "modified"}, "key": "id"}
```

#### Response

```json
{
  "status": "ok",
  "version": 4,
  "affected_rows": 1,
  "new_hash": "xyz789..."
}
```

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Invalid operation |
| 404 | Trial or table not found |
| 409 | ETag mismatch (optimistic locking conflict) |

---

### 6. Create Snapshot

**`POST /trials/{trial_id}/snapshots/{snapshot_name}`**

Create a named snapshot of the current state. Used before golden path execution.

#### Response

```json
{
  "status": "ok",
  "snapshot_name": "pre_golden",
  "version": 4,
  "hash": "abc123..."
}
```

#### Status Codes

| Code | Meaning |
|------|---------|
| 201 | Snapshot created |
| 404 | Trial not found |
| 409 | Snapshot name already exists |

---

### 7. Restore Snapshot

**`POST /trials/{trial_id}/snapshots/{snapshot_name}/restore`**

Restore state from a named snapshot. Used after golden path execution.

#### Response

```json
{
  "status": "ok",
  "restored_from": "pre_golden",
  "version": 5,
  "hash": "abc123..."
}
```

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Restored successfully |
| 404 | Trial or snapshot not found |

---

### 8. Reset to Initial State

**`POST /trials/{trial_id}/reset`**

Reset trial state to the initial state provided during init.

#### Response

```json
{
  "status": "ok",
  "version": 6,
  "hash": "initial_hash..."
}
```

---

### 9. Delete Trial

**`DELETE /trials/{trial_id}`**

Clean up all data for a trial (state, schemas, snapshots).

#### Response

```json
{
  "status": "ok",
  "deleted": {
    "state": true,
    "schemas": 1,
    "unstable_fields": 2,
    "snapshots": 1
  }
}
```

---

### 10. Query State (JSONPath)

**`POST /trials/{trial_id}/query`**

Query state using JSONPath expressions. Preserved from original json-db.

#### Request Body

```json
{
  "jsonpath": "$.reservations[?(@.status=='confirmed')]"
}
```

#### Response

```json
{
  "results": [
    {"id": "RES-001", "user_id": "mia_li_3668", "status": "confirmed"}
  ],
  "count": 1
}
```

---

### 11. SQL Query

**`POST /trials/{trial_id}/sql`**

Execute SQL queries on the state. Preserved from original json-db.

#### Request Body

```json
{
  "query": "SELECT * FROM reservations WHERE status = ?",
  "params": ["confirmed"]
}
```

#### Response

```json
{
  "results": [
    {"id": "RES-001", "user_id": "mia_li_3668", "status": "confirmed"}
  ],
  "count": 1
}
```

---

### 12. Get Schema

**`GET /trials/{trial_id}/schema`**

Get registered schemas and unstable field specifications.

#### Response

```json
{
  "schemas": {
    "reservations": {
      "fields": {"id": "string", "user_id": "string", "status": "string"},
      "primary_key": "id"
    }
  },
  "unstable_fields": [
    {"table_name": "reservations", "field_name": "id", "reason": "auto_id"},
    {"table_name": "reservations", "field_name": "created_at", "reason": "timestamp"}
  ]
}
```

---

### 13. Health Check

**`GET /health`**

Service health check (not trial-specific).

#### Response

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "active_trials": 3
}
```

---

## Internal Data Model

### Trial State Structure

```python
class TrialState:
    """Complete state for a single trial."""
    
    trial_id: str
    
    # Current state: table_name → list of records
    data: Dict[str, List[Dict[str, Any]]]
    
    # Initial state (for reset)
    initial_data: Dict[str, List[Dict[str, Any]]]
    
    # Schema registry: table_name → TableSchema
    schemas: Dict[str, TableSchema]
    
    # Unstable fields: (table_name, field_name) → UnstableFieldSpec
    unstable_fields: Dict[Tuple[str, str], UnstableFieldSpec]
    
    # Named snapshots: snapshot_name → state copy
    snapshots: Dict[str, Dict[str, List[Dict[str, Any]]]]
    
    # Version counter (incremented on each mutation)
    version: int
    
    # SQLite connection for SQL queries
    sql_conn: sqlite3.Connection
```

### In-Memory Storage

```python
class DBService:
    """Main service class."""
    
    # All trial states: trial_id → TrialState
    trials: Dict[str, TrialState] = {}
    
    def get_trial(self, trial_id: str) -> TrialState:
        if trial_id not in self.trials:
            raise TrialNotFoundError(trial_id)
        return self.trials[trial_id]
```

---

## Stable State Filtering Algorithm

The stable state filtering removes fields that produce non-deterministic values:

```python
def get_stable_state(trial: TrialState) -> Dict[str, List[Dict[str, Any]]]:
    """
    Filter out unstable fields from state.
    
    Matches mcp_core.utils.validation.get_stable_database_state()
    """
    stable_state = {}
    
    for table_name, records in trial.data.items():
        stable_records = []
        
        for record in records:
            # Deep copy to avoid modifying original
            stable_record = copy.deepcopy(record)
            
            # Remove unstable fields for this table
            for (tbl, field), spec in trial.unstable_fields.items():
                if tbl == table_name and field in stable_record:
                    del stable_record[field]
            
            stable_records.append(stable_record)
        
        stable_state[table_name] = stable_records
    
    # Convert datetime objects to ISO strings
    return convert_datetime_to_str(stable_state)


def convert_datetime_to_str(data: Any) -> Any:
    """Recursively convert datetime objects to ISO format strings."""
    if isinstance(data, datetime):
        return data.isoformat()
    elif isinstance(data, dict):
        return {key: convert_datetime_to_str(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [convert_datetime_to_str(item) for item in data]
    else:
        return data
```

---

## Hash Computation Algorithm

The hash must be compatible with existing Tau/TlkMcpCore implementations:

```python
def compute_stable_hash(trial: TrialState) -> str:
    """
    Compute SHA-256 hash of stable state.
    
    MUST match mcp_core.utils.validation.calculate_database_hash()
    """
    # Get stable state (unstable fields filtered)
    stable_state = get_stable_state(trial)
    
    # Serialize with deterministic settings
    # - sort_keys=True: deterministic key ordering
    # - separators=(",", ":"): compact JSON, no spaces
    json_str = json.dumps(stable_state, sort_keys=True, separators=(",", ":"))
    
    # SHA-256 with UTF-8 encoding
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()
```

### Compatibility Notes

1. **Key Ordering**: `sort_keys=True` ensures deterministic ordering regardless of Python dict insertion order
2. **Compact JSON**: `separators=(",", ":")` removes whitespace for consistent hashing
3. **UTF-8 Encoding**: Explicit UTF-8 encoding before hashing
4. **Datetime Handling**: All datetime objects converted to ISO 8601 strings before serialization

---

## Trial Isolation Strategy

### Namespace per Trial

Each trial operates in complete isolation:

```
/trials/{trial_id}/...
```

The `trial_id` format is `{task_id}:{trial_index}`, e.g., `airline_task_001:0`.

### Isolation Guarantees

1. **State Isolation**: Each trial has its own data dictionary
2. **Schema Isolation**: Schemas are registered per-trial
3. **Snapshot Isolation**: Snapshots are scoped to their trial
4. **SQL Isolation**: Each trial has its own SQLite connection

### Concurrent Trial Support

Multiple trials can run concurrently without interference:

```python
# Trial 1: airline_task_001:0
POST /trials/airline_task_001:0/init
PATCH /trials/airline_task_001:0/state/reservations

# Trial 2: airline_task_001:1 (same task, different trial)
POST /trials/airline_task_001:1/init
PATCH /trials/airline_task_001:1/state/reservations

# Trial 3: retail_task_002:0 (different task)
POST /trials/retail_task_002:0/init
```

### Cleanup

Trials should be deleted after grading to free memory:

```python
DELETE /trials/airline_task_001:0
```

---

## Changes from Current json-db

### Preserved Endpoints (with trial_id prefix)

| Original | New | Notes |
|----------|-----|-------|
| `POST /reset` | `POST /trials/{trial_id}/init` | Extended with schemas + unstable_fields |
| `GET /dump` | `GET /trials/{trial_id}/state` | Same functionality |
| `POST /query` | `POST /trials/{trial_id}/query` | Same JSONPath support |
| `POST /sql` | `POST /trials/{trial_id}/sql` | Same SQL support |
| `GET /schema` | `GET /trials/{trial_id}/schema` | Extended with unstable_fields |
| `GET /health` | `GET /health` | Unchanged (global) |

### Modified Endpoints

| Original | New | Changes |
|----------|-----|---------|
| `POST /update` | `PATCH /trials/{trial_id}/state/{table}` | Per-table mutations, structured operations |

### New Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /trials/{trial_id}/state/stable` | Stable state (unstable fields filtered) |
| `GET /trials/{trial_id}/state/hash` | Stable hash for grading |
| `POST /trials/{trial_id}/snapshots/{name}` | Create snapshot |
| `POST /trials/{trial_id}/snapshots/{name}/restore` | Restore snapshot |
| `POST /trials/{trial_id}/reset` | Reset to initial state |
| `DELETE /trials/{trial_id}` | Cleanup trial |

### Removed Functionality

- Global state (all state is now per-trial)
- ETag on dump (replaced by version counter)

---

## Grading Flow Integration

The DB Service supports the grading algorithm from [`GRPC_PROTOCOL.md`](docs/GRPC_PROTOCOL.md):

```python
def grade_trial(trial_id: str) -> Grade:
    # 1. Get current trial state hash
    trial_hash = GET /trials/{trial_id}/state/hash → stable_hash
    
    # 2. Snapshot current state before golden path
    POST /trials/{trial_id}/snapshots/pre_golden
    
    # 3. Reset to initial state
    POST /trials/{trial_id}/reset
    
    # 4. Execute golden path actions
    for action in golden_actions:
        execute_tool(trial_id, action.tool_name, action.arguments)
        # Tools call PATCH /trials/{trial_id}/state/{table}
    
    # 5. Get golden state hash
    golden_hash = GET /trials/{trial_id}/state/hash → stable_hash
    
    # 6. Snapshot golden state (needed for diff if mismatch)
    POST /trials/{trial_id}/snapshots/golden_result
    
    # 7. Restore trial state
    POST /trials/{trial_id}/snapshots/pre_golden/restore
    
    # 8. Compare hashes
    if trial_hash == golden_hash:
        return Grade(binary_pass=True, score=1.0)
    else:
        # Get both states for diff
        trial_state = GET /trials/{trial_id}/state/stable
        POST /trials/{trial_id}/snapshots/golden_result/restore
        golden_state = GET /trials/{trial_id}/state/stable
        POST /trials/{trial_id}/snapshots/pre_golden/restore  # restore trial state
        return Grade(binary_pass=False, score=0.0, state_diff=compute_diff(golden_state, trial_state))
```

---

## Error Responses

All error responses follow this format:

```json
{
  "error": "TrialNotFound",
  "message": "Trial 'airline_task_001:0' not found",
  "details": {
    "trial_id": "airline_task_001:0"
  }
}
```

### Error Types

| Error | HTTP Code | Description |
|-------|-----------|-------------|
| `TrialNotFound` | 404 | Trial ID not registered |
| `TrialAlreadyExists` | 409 | Trial ID already initialized |
| `TableNotFound` | 404 | Table name not in state |
| `SnapshotNotFound` | 404 | Snapshot name not found |
| `SnapshotAlreadyExists` | 409 | Snapshot name already used |
| `InvalidOperation` | 400 | Invalid mutation operation |
| `ETagMismatch` | 409 | Optimistic locking conflict |
| `ValidationError` | 400 | Request body validation failed |

---

## Implementation Notes

### Dependencies

```
fastapi>=0.108.0
uvicorn>=0.25.0
jsonpath-ng>=1.6.0
pydantic>=2.0.0
```

### Dockerfile Changes

The existing [`docker/json_db.Dockerfile`](docker/json_db.Dockerfile) requires no changes.
The service code in [`tolokaforge/env/json_db_service/app.py`](tolokaforge/env/json_db_service/app.py) will be extended.

### Thread Safety

For concurrent trial access, use thread-safe data structures:

```python
from threading import Lock

class DBService:
    def __init__(self):
        self.trials: Dict[str, TrialState] = {}
        self._lock = Lock()
    
    def get_or_create_trial(self, trial_id: str) -> TrialState:
        with self._lock:
            if trial_id not in self.trials:
                self.trials[trial_id] = TrialState(trial_id)
            return self.trials[trial_id]
```

---

## Migration Path

### Phase 1: Add New Endpoints
- Add trial-scoped endpoints alongside existing global endpoints
- Existing `/reset`, `/dump`, `/query` continue to work (default trial)

### Phase 2: Update Runner
- Runner uses new `/trials/{trial_id}/init` endpoint
- Tools use `/trials/{trial_id}/state/{table}` for mutations

### Phase 3: Deprecate Global Endpoints
- Remove global `/reset`, `/dump`, `/update`
- All access via trial-scoped endpoints

---

## Testing

### Unit Tests

```python
def test_stable_hash_excludes_unstable_fields():
    """Verify unstable fields are excluded from hash."""
    # Initialize with unstable field spec
    POST /trials/test:0/init
    {
        "tables": {"tickets": []},
        "unstable_fields": [{"table_name": "tickets", "field_name": "id", "reason": "auto_id"}]
    }
    
    # Insert record with unstable field
    PATCH /trials/test:0/state/tickets
    {"operations": [{"op": "insert", "record": {"id": "T-001", "subject": "Help"}}]}
    
    hash1 = GET /trials/test:0/state/hash → stable_hash
    
    # Insert another record with different ID but same stable fields
    POST /trials/test:0/reset
    PATCH /trials/test:0/state/tickets
    {"operations": [{"op": "insert", "record": {"id": "T-999", "subject": "Help"}}]}
    
    hash2 = GET /trials/test:0/state/hash → stable_hash
    
    # Hashes should match (ID is unstable)
    assert hash1 == hash2
```

### Integration Tests

```python
def test_grading_flow():
    """Test full grading flow with snapshot/restore."""
    # Initialize
    POST /trials/grade_test:0/init {...}
    
    # Simulate agent actions
    PATCH /trials/grade_test:0/state/reservations {...}
    agent_hash = GET /trials/grade_test:0/state/hash → stable_hash
    
    # Snapshot agent state before golden path
    POST /trials/grade_test:0/snapshots/pre_golden
    
    # Reset and execute golden path
    POST /trials/grade_test:0/reset
    PATCH /trials/grade_test:0/state/reservations {...}  # golden action
    
    golden_hash = GET /trials/grade_test:0/state/hash → stable_hash
    
    # Snapshot golden state (needed for diff if mismatch)
    POST /trials/grade_test:0/snapshots/golden_result
    
    # Restore agent state
    POST /trials/grade_test:0/snapshots/pre_golden/restore
    
    # Compare
    if agent_hash == golden_hash:
        pass  # success
    else:
        # Get both states for diff
        agent_state = GET /trials/grade_test:0/state/stable
        POST /trials/grade_test:0/snapshots/golden_result/restore
        golden_state = GET /trials/grade_test:0/state/stable
        diff = compute_diff(golden_state, agent_state)
```
