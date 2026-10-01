# DB Service API Specification

The DB Service provides schema-aware JSON state storage, isolated per trial, with
unstable field filtering for hash-based grading. Every store belongs to one trial; the
service keeps no state that trials share.

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
4. **Single-Substrate Digests** — state hashes are `compute_stable_hash` output ([Get Stable Hash](#4-get-stable-hash)); a digest never crosses substrates
5. **Snapshot/Restore** — Supports golden path execution during grading

---

## HTTP API Specification

### Base URL

```
http://db-service:8000
```

All endpoints accept and return JSON. Every data endpoint is addressed by a
`trial_id` path parameter under `/trials/{trial_id}`; only `/health` is global. A path
outside `/trials/{trial_id}` is not served (FastAPI's `404 {"detail": "Not Found"}`),
and a trial must be initialized ([Initialize Trial](#1-initialize-trial)) before any
other endpoint on it answers: until then each one returns `404 TrialNotFound`.

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

The hash is the runner substrate's persisted digest, computed by
[`tolokaforge/core/hash.py::compute_stable_hash`](../tolokaforge/core/hash.py) —
the implementation of record: unstable-field filtering, datetime conversion,
number canonicalization (numeric types always, numeric-looking strings per the
task's `numeric_string_fields` opt-in), then compact sorted JSON and SHA-256.
Every consumer of db-service digests — ETags, snapshot hashes, and the
`ResetTrialResponse.state_hash` / `GetStateResponse.stable_hash` wire fields —
compares only against output of that same function
([`TASK_DESCRIPTION_SCHEMA.md` § Stable State Hash](TASK_DESCRIPTION_SCHEMA.md#stable-state-hash)).

Core grading hashes state in a different algebra by design (`state_digest` in
[`tolokaforge/core/grading/state_checks.py`](../tolokaforge/core/grading/state_checks.py)):
the two agree on which states are equal and label every state differently, so a
hash comparison computes both sides on one substrate and a digest never crosses
substrates — see [`GRADING.md` § Substrate Parity](GRADING.md#substrate-parity).

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
{"op": "upsert", "record": {"account_id": "A1", "symbol": "MSFT", "qty": 99}, "key": ["account_id", "symbol"]}
```

`key` names the record's identity: a single field name (default `"id"` when
omitted) or an ordered list of component names for a composite key. A row
matches when it **carries** every named field with a value equal to the
record's — a row lacking a field never matches, so a single-field `null`
value addresses only rows that store `null`. Components are compared
individually, never as a concatenation — `("a_b", "c")` and `("a", "b_c")`
are distinct keys. The first matching row is updated in place; when no row
matches, `record` is appended.

An upsert whose record omits any named key field is refused with HTTP 400
naming the table, the field(s), the record's keys, and the zero-based
operation index; a composite key additionally refuses a `null` component (a
`null` component cannot address a row) and an empty `key` list. Every
upsert in a batch — including one missing its `record` — is validated
before any operation is applied, so a batch refused over an upsert mutates
nothing: rows, version, and the SQL mirror are unchanged.

```json
{
  "error": "InvalidOperation",
  "message": "Upsert record for table 'positions' does not contain key field 'account_id' (operation 0); record keys: ['qty', 'symbol']",
  "details": {
    "table_name": "positions",
    "missing_components": ["account_id"],
    "record_keys": ["qty", "symbol"],
    "op_index": 0
  }
}
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

Query state using JSONPath expressions.

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

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success (an expression that matches nothing returns `"results": []`) |
| 400 | `InvalidJSONPath`: the expression does not parse, or a filter regex / string function in it cannot be evaluated |
| 404 | Trial not found |
| 422 | Request body validation failed (e.g. `jsonpath` missing) |

Any other failure is a 500.

---

### 11. Update State (JSONPath)

**`POST /trials/{trial_id}/update`**

Apply a batch of JSONPath ops to the trial state. The builtin `db_update` tool
writes through this endpoint.

#### Request Body

```json
{
  "ops": [
    {"op": "replace", "path": "$.tickets[0].status", "value": "closed"},
    {"op": "add", "path": "$.audit_log.-", "value": {"ticket_id": "T-100", "action": "close"}}
  ]
}
```

`path` is a JSONPath starting with `$`. Unknown keys on the body or on an op
are refused with 422.

| Op | Effect |
|----|--------|
| `replace` | Sets every match of `path` to `value`. A path that matches nothing, or that matches the root `$`, refuses the batch. |
| `add` | Parses `path` as JSONPath and resolves only its parent. The path must end in one key name, which is the key as JSONPath reads it, so `$.tickets[0]."note"` and `$.tickets[0].note` both set `note`, and a query on the same path finds it. A path ending in anything else refuses the batch: the root `$`, an index (`$.tickets[0]`), a filter, a wildcard, a union (`a|b`) or more than one key (`a,b`). The one form JSONPath does not parse is a trailing `.-`, which appends (`$.tickets.-`). Every parent match must be a dict, which gains the key set to `value`, or a list, which has `value` appended whatever the key. A parent that matches nothing, or matches a scalar, refuses the batch. |
| `remove` | Deletes every match of `path` from its parent dict or list. A path that matches the root `$` refuses the batch. A path that matches nothing deletes nothing and still commits: `version` increments and `stable_hash` is unchanged. |

#### Atomicity

The ops run in order on a copy of the state. The batch commits only when every
op succeeds, every top-level key still holds a list of row objects
(`{"<table>": [{...}, ...]}`), the shape every state reader and grader
requires, **and** the SQL mirror can store the result. A refused batch changes
nothing: rows, `version` and the SQL mirror are as they were. A committed batch
re-syncs the SQL mirror and increments `version` once.

The SQL mirror refuses a state whose values or keys SQLite cannot hold: an
integer outside the signed 64-bit range, a string or key holding a lone UTF-16
surrogate (which has no UTF-8 encoding), or two keys of one table (or two
table names) that differ only in letter case, since SQLite identifiers are
case-insensitive.

#### Response

```json
{
  "status": "ok",
  "version": 5,
  "stable_hash": "abc123..."
}
```

#### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | `InvalidJSONPath`: a path does not start with `$`, does not parse, or cannot be evaluated. `InvalidOperation`: an unknown op, a `replace` / `add` / `remove` the op table above refuses, an op that would leave a top-level key that is not a list or a row that is not an object, or a result the SQL mirror cannot store |
| 404 | Trial not found |
| 422 | Request body validation failed (e.g. `ops` not a list, or an op missing `op` / `path`) |

Any other failure is a 500.

A 400's message names the zero-based op index and the reason, and the path as
the client sent it; an `InvalidJSONPath` message also gives an example path.
`details` carries `op_index` and `path` (plus `op` for `InvalidOperation`). A
refusal by the SQL mirror concerns the whole batch: its message names the
table, and the row, field and value where one is to blame, and `details`
carries `table`:

```json
{
  "error": "InvalidJSONPath",
  "message": "op 0: path '/tickets/0/status' is not a JSONPath; paths are JSONPath, e.g. '$.tickets[0].status'",
  "details": {"path": "/tickets/0/status", "op_index": 0}
}
```

---

### 12. SQL Query

**`POST /trials/{trial_id}/sql`**

Execute SQL queries on the state.

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

### 13. Get Schema

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

### 14. Health Check

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

The service hashes stable state with
[`tolokaforge/core/hash.py::compute_stable_hash`](../tolokaforge/core/hash.py) —
see [Get Stable Hash](#4-get-stable-hash) for the algorithm, its scope, and the
substrate invariant.

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
| `InvalidOperation` | 400 | Invalid mutation or update operation |
| `InvalidJSONPath` | 400 | A path is not a JSONPath, does not parse, or cannot be evaluated |
| `ETagMismatch` | 409 | Optimistic locking conflict |

A request body that fails validation is refused with FastAPI's 422 shape
instead: `{"detail": [{"loc": ["body", "ops", 0, "op"], "msg": "Field required", ...}]}`.

---

## Implementation Notes

### Dependencies

```
fastapi>=0.108.0
uvicorn>=0.25.0
jsonpath-ng>=1.6.0
pydantic>=2.0.0
```

### Container

The service ships as the image built from [`tolokaforge/docker/dockerfiles/db_service.Dockerfile`](../tolokaforge/docker/dockerfiles/db_service.Dockerfile);
its code is [`tolokaforge/env/json_db_service/app.py`](../tolokaforge/env/json_db_service/app.py).

### Thread Safety

`DBService` guards its trial map with one lock. It never creates a trial implicitly:
`create_trial` (behind `POST /trials/{trial_id}/init`) refuses an id that exists
(`409 TrialAlreadyExists`), and `get_trial` (behind every other trial endpoint) raises
`TrialNotFound` for an id nobody initialized. Each trial carries its own lock, held for
the whole of a request on it, so concurrent requests on different trials do not
serialize on each other.

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
