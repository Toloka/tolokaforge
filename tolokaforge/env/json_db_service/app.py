"""JSON DB service - REST API for versioned JSON state with SQL support and trial isolation.

This module provides a schema-aware JSON state storage with:
- Trial isolation: Each trial has isolated state, schemas, and snapshots
- Unstable field filtering: Explicit field exclusion for deterministic hashing
- Snapshot/Restore: Supports golden path execution during grading
- SQL queries: SQLite-based querying on JSON data
- JSONPath queries: Query state using JSONPath expressions
"""

import copy
import hashlib
import json
import logging
import sqlite3
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from jsonpath_ng import parse
from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)

# Import hash functions from core module
# Note: In Docker container, this import path works because tolokaforge is installed
try:
    from tolokaforge.core.hash import compute_stable_hash, filter_unstable_fields
except ImportError:
    # Fallback for standalone testing - implement locally
    logger.warning(
        "Could not import tolokaforge.core.hash, using local fallback implementation. "
        "This is expected in standalone testing but should not occur in production."
    )

    def _convert_datetime_to_str(data: Any) -> Any:
        """Recursively convert datetime objects to ISO format strings."""
        from datetime import datetime

        if isinstance(data, datetime):
            return data.isoformat()
        elif isinstance(data, dict):
            return {key: _convert_datetime_to_str(value) for key, value in data.items()}
        elif isinstance(data, list):
            return [_convert_datetime_to_str(item) for item in data]
        elif isinstance(data, set):
            return sorted([_convert_datetime_to_str(item) for item in data])
        else:
            return data

    def filter_unstable_fields(
        state: dict[str, Any],
        unstable_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Filter out unstable fields from state dictionary."""
        if not unstable_fields:
            return state

        top_level_fields: set = set()
        nested_patterns: dict[str, list[str]] = {}

        for field_spec in unstable_fields:
            if "." in field_spec:
                parts = field_spec.split(".", 1)
                table = parts[0]
                nested_field = parts[1]
                if table not in nested_patterns:
                    nested_patterns[table] = []
                nested_patterns[table].append(nested_field)
            else:
                top_level_fields.add(field_spec)

        def filter_dict(d: dict[str, Any], parent_key: str = "") -> dict[str, Any]:
            result = {}
            for key, value in d.items():
                if key in top_level_fields:
                    continue

                if isinstance(value, dict):
                    if key in nested_patterns:
                        filtered_value = {
                            k: v for k, v in value.items() if k not in nested_patterns[key]
                        }
                        result[key] = filter_dict(filtered_value, key)
                    else:
                        result[key] = filter_dict(value, key)
                elif isinstance(value, list):
                    if value and isinstance(value[0], dict):
                        if key in nested_patterns:
                            result[key] = [
                                {k: v for k, v in item.items() if k not in nested_patterns[key]}
                                for item in value
                            ]
                        else:
                            result[key] = value
                    else:
                        result[key] = value
                else:
                    result[key] = value

            return result

        return filter_dict(state)

    def compute_stable_hash(
        state: dict[str, Any],
        unstable_fields: list[str] | None = None,
    ) -> str:
        """Compute a stable SHA-256 hash of the state dictionary."""
        if unstable_fields:
            state = filter_unstable_fields(state, unstable_fields)

        serializable_state = _convert_datetime_to_str(state)
        json_str = json.dumps(
            serializable_state, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


app = FastAPI(title="JSON DB Service", version="1.0.0")


# =============================================================================
# Pydantic Models for Request/Response
# =============================================================================


class QueryRequest(BaseModel):
    """JSONPath query request"""

    jsonpath: str


class SQLRequest(BaseModel):
    """SQL query request"""

    query: str
    params: list[Any] | None = None


class TableSchema(BaseModel):
    """Table schema definition"""

    table_name: str
    fields: dict[str, str]  # field_name -> type
    primary_key: str | None = "id"


class UnstableFieldSpec(BaseModel):
    """Unstable field specification"""

    table_name: str
    field_name: str
    reason: str | None = None  # "auto_id", "timestamp", "llm_generated", "random"


class InitRequest(BaseModel):
    """Trial initialization request"""

    tables: dict[str, list[dict[str, Any]]]
    schemas: list[TableSchema] | None = None
    unstable_fields: list[UnstableFieldSpec] | None = None


class MutationOperation(BaseModel):
    """Single mutation operation"""

    op: str  # "insert", "update", "delete", "upsert"
    record: dict[str, Any] | None = None  # for insert/upsert
    filter: dict[str, Any] | None = None  # for update/delete
    set: dict[str, Any] | None = None  # for update
    key: str | None = None  # for upsert


class MutationRequest(BaseModel):
    """Mutation request with operations"""

    operations: list[MutationOperation]
    etag: str | None = None


# Legacy models for backward compatibility
class UpdateOp(BaseModel):
    """Update operation (legacy)"""

    op: str  # "replace", "add", "remove"
    path: str  # JSONPath
    value: Any | None = None


class UpdateRequest(BaseModel):
    """Update request (legacy)"""

    ops: list[UpdateOp]
    etag: str | None = None


# =============================================================================
# Trial State Management
# =============================================================================


class TrialState(BaseModel):
    """Complete state for a single trial."""

    trial_id: str

    # Current state: table_name -> list of records
    data: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    # Initial state (for reset)
    initial_data: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    # Schema registry: table_name -> TableSchema
    schemas: dict[str, TableSchema] = Field(default_factory=dict)

    # Unstable fields: (table_name, field_name) -> UnstableFieldSpec
    unstable_fields: dict[tuple[str, str], UnstableFieldSpec] = Field(default_factory=dict)

    # Named snapshots: snapshot_name -> state copy
    snapshots: dict[str, dict[str, list[dict[str, Any]]]] = Field(default_factory=dict)

    # Version counter (incremented on each mutation)
    version: int = 0

    # SQLite connection for SQL queries (private, excluded from serialization)
    _sql_conn: Any | None = PrivateAttr(default=None)

    # Lock for thread-safe access (private, excluded from serialization)
    _lock: Lock = PrivateAttr(default_factory=Lock)

    model_config = {"extra": "forbid"}

    def model_post_init(self, __context: Any) -> None:
        """Initialize SQLite connection after model creation."""
        self._init_sql_db()

    def _init_sql_db(self):
        """Initialize in-memory SQLite database."""
        self._sql_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._sql_conn.row_factory = sqlite3.Row

    def sync_json_to_sql(self):
        """Sync JSON data to SQL tables.

        BUG FIX: Previously only looked at first record to infer schema.
        Now scans ALL records to build complete schema with all possible columns.
        This handles inconsistent records (e.g., some devices have last_esim_transfer_date, others don't).
        """
        if not self._sql_conn:
            self._init_sql_db()

        assert self._sql_conn is not None  # For type checker
        cursor = self._sql_conn.cursor()

        # Drop all existing tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        for table in tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table[0]}"')

        # Create tables from JSON structure
        for table_name, table_data in self.data.items():
            if isinstance(table_data, list) and len(table_data) > 0:
                # BUG FIX: Scan ALL records to build complete schema
                # Records may have inconsistent fields - we need the union of all fields
                all_columns: dict[str, str] = {}  # column_name -> sql_type
                for record in table_data:
                    if isinstance(record, dict):
                        for key, value in record.items():
                            if key not in all_columns:
                                all_columns[key] = self._infer_sql_type(value)
                            # If we already have this column but current value gives better type info
                            # (e.g., previous was None -> TEXT, now we have an int)
                            elif all_columns[key] == "TEXT" and value is not None:
                                inferred = self._infer_sql_type(value)
                                if inferred != "TEXT":
                                    all_columns[key] = inferred

                if all_columns:
                    columns = [f'"{key}" {col_type}' for key, col_type in all_columns.items()]
                    create_sql = f'CREATE TABLE "{table_name}" ({", ".join(columns)})'
                    cursor.execute(create_sql)

                    # Insert all records using the complete column list
                    column_names = list(all_columns.keys())
                    placeholders = ", ".join(["?" for _ in column_names])
                    keys = ", ".join([f'"{k}"' for k in column_names])
                    insert_sql = f'INSERT INTO "{table_name}" ({keys}) VALUES ({placeholders})'

                    for record in table_data:
                        if isinstance(record, dict):
                            # Use None for missing columns, serialize complex types
                            values = [
                                self._serialize_for_sql(record.get(col)) for col in column_names
                            ]
                            cursor.execute(insert_sql, values)

        self._sql_conn.commit()

    def _serialize_for_sql(self, value: Any) -> Any:
        """Serialize a value for SQLite storage.

        BUG FIX: SQLite can't handle complex Python types (lists, dicts).
        These need to be serialized to JSON strings.
        """
        if value is None:
            return None
        elif isinstance(value, (list, dict)):
            # Serialize complex types to JSON strings
            return json.dumps(value, default=str)
        elif isinstance(value, bool):
            # SQLite stores bools as integers
            return int(value)
        else:
            return value

    def _infer_sql_type(self, value: Any) -> str:
        """Infer SQL type from Python value."""
        if isinstance(value, bool) or isinstance(value, int):
            return "INTEGER"
        elif isinstance(value, float):
            return "REAL"
        elif isinstance(value, (list, dict)):
            # Complex types are stored as JSON TEXT
            return "TEXT"
        elif value is None:
            return "TEXT"
        else:
            return "TEXT"

    def execute_sql(self, query: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute SQL query and return results."""
        if not self._sql_conn:
            self._init_sql_db()

        assert self._sql_conn is not None  # For type checker
        cursor = self._sql_conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)

        columns = (
            [description[0] for description in cursor.description] if cursor.description else []
        )
        results = []
        for row in cursor.fetchall():
            results.append(dict(zip(columns, row)))

        return results

    def get_unstable_field_list(self) -> list[str]:
        """Get unstable fields as list of 'table.field' strings for hash computation.

        Handles singular/plural table name mismatches by trying to match unstable field
        table names against actual data table names using various strategies:
        - Exact match
        - Adding 's' suffix (singular -> plural)
        - Removing 's' suffix (plural -> singular)
        - Suffix matching (for prefixed table names)
        """
        result = []
        data_tables = set(self.data.keys())

        for table, field in self.unstable_fields:
            matched_table = self._resolve_table_name(table, data_tables)
            if matched_table:
                result.append(f"{matched_table}.{field}")
                if matched_table != table:
                    logger.debug(
                        f"Unstable field table name resolved: '{table}' -> '{matched_table}'"
                    )
            else:
                # Fall back to original table name if no match found
                result.append(f"{table}.{field}")
                logger.warning(
                    f"Unstable field table '{table}' not found in data tables: {list(data_tables)}"
                )

        return result

    def _resolve_table_name(self, table: str, data_tables: set) -> str | None:
        """Resolve unstable field table name to actual data table name.

        Tries multiple matching strategies to handle singular/plural mismatches.

        Args:
            table: The table name from unstable fields registration
            data_tables: Set of actual table names in self.data

        Returns:
            Matched data table name, or None if no match found
        """
        # Strategy 1: Exact match
        if table in data_tables:
            return table

        # Strategy 2: Try adding 's' (singular -> plural)
        plural_form = table + "s"
        if plural_form in data_tables:
            return plural_form

        # Strategy 3: Try removing 's' (plural -> singular)
        if table.endswith("s"):
            singular_form = table[:-1]
            if singular_form in data_tables:
                return singular_form

        # Strategy 4: Suffix matching - find data table that ends with the unstable table name
        # This handles cases like "servicenow_csm_sn_customerservice_cases" matching
        # against "sn_customerservice_case" or vice versa
        for data_table in data_tables:
            # Check if data_table ends with the unstable table name
            if data_table.endswith(table):
                return data_table
            # Check if data_table ends with singular form of unstable table
            if table.endswith("s") and data_table.endswith(table[:-1]):
                return data_table
            # Check if unstable table ends with data_table name
            if table.endswith(data_table):
                return data_table
            # Check if unstable table (minus 's') ends with data_table
            if table.endswith("s") and table[:-1].endswith(data_table):
                return data_table
            # Check if data_table (plus 's') matches unstable table suffix
            if table.endswith(data_table + "s"):
                return data_table

        return None

    def get_stable_state(self) -> dict[str, list[dict[str, Any]]]:
        """Get state with unstable fields filtered out."""
        unstable_list = self.get_unstable_field_list()
        return filter_unstable_fields(self.data, unstable_list)

    def compute_full_hash(self) -> str:
        """Compute hash of full state (including unstable fields)."""
        return compute_stable_hash(self.data)

    def compute_stable_hash(self) -> str:
        """Compute hash of stable state (unstable fields filtered)."""
        unstable_list = self.get_unstable_field_list()
        return compute_stable_hash(self.data, unstable_list)

    def cleanup(self):
        """Clean up resources."""
        if self._sql_conn:
            self._sql_conn.close()
            self._sql_conn = None


class DBService:
    """Main service class managing all trials."""

    def __init__(self):
        self.trials: dict[str, TrialState] = {}
        self._lock = Lock()

    def get_trial(self, trial_id: str) -> TrialState:
        """Get trial by ID, raises if not found."""
        with self._lock:
            if trial_id not in self.trials:
                raise TrialNotFoundError(trial_id)
            return self.trials[trial_id]

    def create_trial(self, trial_id: str) -> TrialState:
        """Create a new trial, raises if already exists."""
        with self._lock:
            if trial_id in self.trials:
                raise TrialAlreadyExistsError(trial_id)
            trial = TrialState(trial_id=trial_id)
            self.trials[trial_id] = trial
            return trial

    def delete_trial(self, trial_id: str) -> dict[str, Any]:
        """Delete a trial and return cleanup info."""
        with self._lock:
            if trial_id not in self.trials:
                raise TrialNotFoundError(trial_id)
            trial = self.trials[trial_id]
            deleted_info = {
                "state": True,
                "schemas": len(trial.schemas),
                "unstable_fields": len(trial.unstable_fields),
                "snapshots": len(trial.snapshots),
            }
            trial.cleanup()
            del self.trials[trial_id]
            return deleted_info

    def get_active_trial_count(self) -> int:
        """Get count of active trials."""
        with self._lock:
            return len(self.trials)


# =============================================================================
# Custom Exceptions
# =============================================================================


class TrialNotFoundError(Exception):
    """Trial not found."""

    def __init__(self, trial_id: str):
        self.trial_id = trial_id
        super().__init__(f"Trial '{trial_id}' not found")


class TrialAlreadyExistsError(Exception):
    """Trial already exists."""

    def __init__(self, trial_id: str):
        self.trial_id = trial_id
        super().__init__(f"Trial '{trial_id}' already exists")


class TableNotFoundError(Exception):
    """Table not found."""

    def __init__(self, table_name: str):
        self.table_name = table_name
        super().__init__(f"Table '{table_name}' not found")


class SnapshotNotFoundError(Exception):
    """Snapshot not found."""

    def __init__(self, snapshot_name: str):
        self.snapshot_name = snapshot_name
        super().__init__(f"Snapshot '{snapshot_name}' not found")


class SnapshotAlreadyExistsError(Exception):
    """Snapshot already exists."""

    def __init__(self, snapshot_name: str):
        self.snapshot_name = snapshot_name
        super().__init__(f"Snapshot '{snapshot_name}' already exists")


# =============================================================================
# Error Response Helpers
# =============================================================================


def error_response(error_type: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    """Create structured error response."""
    return {"error": error_type, "message": message, "details": details}


def handle_trial_not_found(e: TrialNotFoundError):
    """Handle TrialNotFoundError."""
    raise HTTPException(
        status_code=404,
        detail=error_response("TrialNotFound", str(e), {"trial_id": e.trial_id}),
    )


def handle_trial_already_exists(e: TrialAlreadyExistsError):
    """Handle TrialAlreadyExistsError."""
    raise HTTPException(
        status_code=409,
        detail=error_response("TrialAlreadyExists", str(e), {"trial_id": e.trial_id}),
    )


def handle_table_not_found(e: TableNotFoundError):
    """Handle TableNotFoundError."""
    raise HTTPException(
        status_code=404,
        detail=error_response("TableNotFound", str(e), {"table_name": e.table_name}),
    )


def handle_snapshot_not_found(e: SnapshotNotFoundError):
    """Handle SnapshotNotFoundError."""
    raise HTTPException(
        status_code=404,
        detail=error_response("SnapshotNotFound", str(e), {"snapshot_name": e.snapshot_name}),
    )


def handle_snapshot_already_exists(e: SnapshotAlreadyExistsError):
    """Handle SnapshotAlreadyExistsError."""
    raise HTTPException(
        status_code=409,
        detail=error_response("SnapshotAlreadyExists", str(e), {"snapshot_name": e.snapshot_name}),
    )


# =============================================================================
# Global Service Instance
# =============================================================================

db_service = DBService()

# Default trial ID for backward compatibility
DEFAULT_TRIAL_ID = "__default__"


def get_or_create_default_trial() -> TrialState:
    """Get or create the default trial for backward compatibility."""
    try:
        return db_service.get_trial(DEFAULT_TRIAL_ID)
    except TrialNotFoundError:
        return db_service.create_trial(DEFAULT_TRIAL_ID)


# =============================================================================
# Trial-Scoped Endpoints
# =============================================================================


@app.post("/trials/{trial_id}/init")
async def init_trial(trial_id: str, req: InitRequest) -> dict[str, Any]:
    """Initialize a trial with initial state, schemas, and unstable field specifications."""
    logger.info("Initializing trial", extra={"trial_id": trial_id, "num_tables": len(req.tables)})
    try:
        trial = db_service.create_trial(trial_id)
    except TrialAlreadyExistsError as e:
        logger.warning("Trial already exists", extra={"trial_id": trial_id})
        handle_trial_already_exists(e)

    with trial._lock:
        # Set initial data
        trial.data = copy.deepcopy(req.tables)
        trial.initial_data = copy.deepcopy(req.tables)

        # Register schemas
        if req.schemas:
            for schema in req.schemas:
                trial.schemas[schema.table_name] = schema

        # Register unstable fields
        if req.unstable_fields:
            for spec in req.unstable_fields:
                trial.unstable_fields[(spec.table_name, spec.field_name)] = spec

        # Sync to SQL
        trial.sync_json_to_sql()
        trial.version = 1

        logger.info(
            "Trial initialized successfully",
            extra={
                "trial_id": trial_id,
                "tables": list(req.tables.keys()),
                "schemas_count": len(trial.schemas),
                "unstable_fields_count": len(trial.unstable_fields),
            },
        )

        return {
            "status": "ok",
            "trial_id": trial_id,
            "tables_initialized": list(req.tables.keys()),
            "schemas_registered": len(trial.schemas),
            "unstable_fields_registered": len(trial.unstable_fields),
            "initial_hash": trial.compute_stable_hash(),
        }


@app.get("/trials/{trial_id}/state")
async def get_state(
    trial_id: str, tables: str | None = Query(None, description="Comma-separated table names")
) -> dict[str, Any]:
    """Get the complete current state including all fields."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        data = trial.data
        if tables:
            table_list = [t.strip() for t in tables.split(",")]
            data = {k: v for k, v in trial.data.items() if k in table_list}

        return {
            "data": data,
            "version": trial.version,
            "full_hash": trial.compute_full_hash(),
            "stable_hash": trial.compute_stable_hash(),
        }


@app.get("/trials/{trial_id}/state/stable")
async def get_stable_state(trial_id: str) -> dict[str, Any]:
    """Get state with unstable fields filtered out."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        stable_data = trial.get_stable_state()
        filtered_fields = [
            {"table": table, "field": field} for (table, field) in trial.unstable_fields
        ]

        return {
            "data": stable_data,
            "version": trial.version,
            "stable_hash": trial.compute_stable_hash(),
            "filtered_fields": filtered_fields,
        }


@app.get("/trials/{trial_id}/state/hash")
async def get_state_hash(trial_id: str) -> dict[str, Any]:
    """Get SHA-256 hash of the stable state."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        return {
            "stable_hash": trial.compute_stable_hash(),
            "full_hash": trial.compute_full_hash(),
            "version": trial.version,
        }


@app.patch("/trials/{trial_id}/state/{table_name}")
async def mutate_state(trial_id: str, table_name: str, req: MutationRequest) -> dict[str, Any]:
    """Apply mutations to a specific table.

    Note: If the table doesn't exist and the first operation is an insert or upsert,
    the table will be auto-created. This allows tools to create new records in tables
    that weren't initialized during trial init.
    """
    logger.debug(
        "Mutating state",
        extra={
            "trial_id": trial_id,
            "table_name": table_name,
            "num_operations": len(req.operations),
        },
    )
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        logger.warning("Trial not found for mutation", extra={"trial_id": trial_id})
        handle_trial_not_found(e)

    with trial._lock:
        # Check if table exists - auto-create if first operation is insert/upsert
        if table_name not in trial.data:
            # Check if we can auto-create the table (first op must be insert or upsert)
            if req.operations and req.operations[0].op in ("insert", "upsert"):
                logger.info(
                    "Auto-creating table for insert/upsert operation",
                    extra={"trial_id": trial_id, "table_name": table_name},
                )
                trial.data[table_name] = []
            else:
                logger.warning(
                    "Table not found for mutation",
                    extra={"trial_id": trial_id, "table_name": table_name},
                )
                raise HTTPException(
                    status_code=404,
                    detail=error_response(
                        "TableNotFound",
                        f"Table '{table_name}' not found",
                        {"table_name": table_name},
                    ),
                )

        # Check ETag for optimistic locking
        if req.etag and req.etag != trial.compute_full_hash():
            logger.warning(
                "ETag mismatch during mutation",
                extra={"trial_id": trial_id, "expected_etag": req.etag},
            )
            raise HTTPException(
                status_code=409,
                detail=error_response(
                    "ETagMismatch", "State was modified", {"expected_etag": req.etag}
                ),
            )

        affected_rows = 0
        table_data = trial.data[table_name]

        for op in req.operations:
            if op.op == "insert":
                if op.record is None:
                    raise HTTPException(
                        status_code=400,
                        detail=error_response(
                            "InvalidOperation", "Insert requires 'record'", {"op": op.op}
                        ),
                    )
                table_data.append(copy.deepcopy(op.record))
                affected_rows += 1

            elif op.op == "update":
                if op.filter is None or op.set is None:
                    raise HTTPException(
                        status_code=400,
                        detail=error_response(
                            "InvalidOperation", "Update requires 'filter' and 'set'", {"op": op.op}
                        ),
                    )
                for record in table_data:
                    if all(record.get(k) == v for k, v in op.filter.items()):
                        record.update(op.set)
                        affected_rows += 1

            elif op.op == "delete":
                if op.filter is None:
                    raise HTTPException(
                        status_code=400,
                        detail=error_response(
                            "InvalidOperation", "Delete requires 'filter'", {"op": op.op}
                        ),
                    )
                original_len = len(table_data)
                trial.data[table_name] = [
                    r for r in table_data if not all(r.get(k) == v for k, v in op.filter.items())
                ]
                affected_rows += original_len - len(trial.data[table_name])
                table_data = trial.data[table_name]

            elif op.op == "upsert":
                if op.record is None:
                    raise HTTPException(
                        status_code=400,
                        detail=error_response(
                            "InvalidOperation", "Upsert requires 'record'", {"op": op.op}
                        ),
                    )
                key_field = op.key or "id"
                key_value = op.record.get(key_field)
                found = False
                for record in table_data:
                    if record.get(key_field) == key_value:
                        record.update(op.record)
                        found = True
                        affected_rows += 1
                        break
                if not found:
                    table_data.append(copy.deepcopy(op.record))
                    affected_rows += 1

            else:
                logger.error(
                    "Unknown mutation operation",
                    extra={"trial_id": trial_id, "op": op.op},
                )
                raise HTTPException(
                    status_code=400,
                    detail=error_response(
                        "InvalidOperation", f"Unknown operation: {op.op}", {"op": op.op}
                    ),
                )

        trial.version += 1
        trial.sync_json_to_sql()

        logger.debug(
            "Mutation completed",
            extra={
                "trial_id": trial_id,
                "table_name": table_name,
                "affected_rows": affected_rows,
                "new_version": trial.version,
            },
        )

        return {
            "status": "ok",
            "version": trial.version,
            "affected_rows": affected_rows,
            "new_hash": trial.compute_stable_hash(),
        }


@app.post("/trials/{trial_id}/snapshots/{snapshot_name}", status_code=201)
async def create_snapshot(trial_id: str, snapshot_name: str) -> dict[str, Any]:
    """Create a named snapshot of the current state."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        if snapshot_name in trial.snapshots:
            raise HTTPException(
                status_code=409,
                detail=error_response(
                    "SnapshotAlreadyExists",
                    f"Snapshot '{snapshot_name}' already exists",
                    {"snapshot_name": snapshot_name},
                ),
            )

        trial.snapshots[snapshot_name] = copy.deepcopy(trial.data)

        return {
            "status": "ok",
            "snapshot_name": snapshot_name,
            "version": trial.version,
            "hash": trial.compute_stable_hash(),
        }


@app.post("/trials/{trial_id}/snapshots/{snapshot_name}/restore")
async def restore_snapshot(trial_id: str, snapshot_name: str) -> dict[str, Any]:
    """Restore state from a named snapshot."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        if snapshot_name not in trial.snapshots:
            raise HTTPException(
                status_code=404,
                detail=error_response(
                    "SnapshotNotFound",
                    f"Snapshot '{snapshot_name}' not found",
                    {"snapshot_name": snapshot_name},
                ),
            )

        trial.data = copy.deepcopy(trial.snapshots[snapshot_name])
        trial.version += 1
        trial.sync_json_to_sql()

        return {
            "status": "ok",
            "restored_from": snapshot_name,
            "version": trial.version,
            "hash": trial.compute_stable_hash(),
        }


@app.post("/trials/{trial_id}/reset")
async def reset_trial(trial_id: str) -> dict[str, Any]:
    """Reset trial state to the initial state provided during init."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        trial.data = copy.deepcopy(trial.initial_data)
        trial.version += 1
        trial.sync_json_to_sql()

        return {
            "status": "ok",
            "version": trial.version,
            "hash": trial.compute_stable_hash(),
        }


@app.delete("/trials/{trial_id}")
async def delete_trial(trial_id: str) -> dict[str, Any]:
    """Clean up all data for a trial."""
    try:
        deleted_info = db_service.delete_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    return {"status": "ok", "deleted": deleted_info}


@app.post("/trials/{trial_id}/query")
async def query_trial(trial_id: str, req: QueryRequest) -> dict[str, Any]:
    """Query state using JSONPath expressions."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        try:
            jsonpath_expr = parse(req.jsonpath)
            matches = jsonpath_expr.find(trial.data)
            results = [match.value for match in matches]
            return {"results": results, "count": len(results)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Query failed: {str(e)}")


@app.post("/trials/{trial_id}/sql")
async def sql_query_trial(trial_id: str, req: SQLRequest) -> dict[str, Any]:
    """Execute SQL query on the trial state."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        try:
            results = trial.execute_sql(req.query, req.params)
            return {"results": results, "count": len(results)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"SQL query failed: {str(e)}")


@app.get("/trials/{trial_id}/schema")
async def get_trial_schema(trial_id: str) -> dict[str, Any]:
    """Get registered schemas and unstable field specifications."""
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        schemas_dict = {}
        for table_name, schema in trial.schemas.items():
            schemas_dict[table_name] = {
                "fields": schema.fields,
                "primary_key": schema.primary_key,
            }

        unstable_list = [
            {"table_name": spec.table_name, "field_name": spec.field_name, "reason": spec.reason}
            for spec in trial.unstable_fields.values()
        ]

        return {"schemas": schemas_dict, "unstable_fields": unstable_list}


# =============================================================================
# Global Health Check
# =============================================================================


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check (not trial-specific)."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "active_trials": db_service.get_active_trial_count(),
    }


# =============================================================================
# Backward Compatibility Endpoints (using default trial)
# =============================================================================


@app.post("/reset")
async def reset_legacy(initial_state: dict[str, Any]) -> dict[str, str]:
    """Reset database with initial state (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        trial.data = copy.deepcopy(initial_state)
        trial.initial_data = copy.deepcopy(initial_state)
        trial.version += 1
        trial.sync_json_to_sql()

        return {"status": "ok", "etag": trial.compute_full_hash()}


@app.post("/query")
async def query_legacy(req: QueryRequest) -> dict[str, Any]:
    """Query database using JSONPath (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        try:
            jsonpath_expr = parse(req.jsonpath)
            matches = jsonpath_expr.find(trial.data)
            results = [match.value for match in matches]
            return {"results": results, "count": len(results)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Query failed: {str(e)}")


@app.post("/update")
async def update_legacy(req: UpdateRequest) -> dict[str, Any]:
    """Update database with operations (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        # Check etag for optimistic locking
        if req.etag and req.etag != trial.compute_full_hash():
            raise HTTPException(status_code=409, detail="ETag mismatch - state was modified")

        before_data = json.dumps(trial.data, sort_keys=True)

        try:
            for op in req.ops:
                jsonpath_expr = parse(op.path)

                if op.op == "replace":
                    matches = jsonpath_expr.find(trial.data)
                    if not matches:
                        raise ValueError(f"Path not found: {op.path}")
                    for match in matches:
                        match.full_path.update(trial.data, op.value)

                elif op.op == "add":
                    # Add new value at path
                    parent_path = ".".join(op.path.split(".")[:-1])
                    key = op.path.split(".")[-1]
                    if parent_path:
                        parent_expr = parse(parent_path)
                        parents = parent_expr.find(trial.data)
                        for parent in parents:
                            if isinstance(parent.value, dict):
                                parent.value[key] = op.value
                            elif isinstance(parent.value, list):
                                parent.value.append(op.value)
                    else:
                        trial.data[key] = op.value

                elif op.op == "remove":
                    matches = jsonpath_expr.find(trial.data)
                    for match in matches:
                        parent = match.context.value
                        if isinstance(parent, dict) and match.path.fields:
                            del parent[match.path.fields[0]]
                        elif isinstance(parent, list):
                            parent.remove(match.value)

                else:
                    raise ValueError(f"Unknown operation: {op.op}")

            trial.version += 1
            trial.sync_json_to_sql()
            after_data = json.dumps(trial.data, sort_keys=True)

            return {
                "status": "ok",
                "etag": trial.compute_full_hash(),
                "version": trial.version,
                "diff": {"before": before_data, "after": after_data},
            }

        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Update failed: {str(e)}")


@app.get("/dump")
async def dump_legacy() -> dict[str, Any]:
    """Get normalized database dump (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        return {
            "data": trial.data,
            "etag": trial.compute_full_hash(),
            "version": trial.version,
            "normalized": json.dumps(trial.data, sort_keys=True, indent=2),
        }


@app.post("/sql")
async def sql_query_legacy(req: SQLRequest) -> dict[str, Any]:
    """Execute SQL query on the database (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        try:
            results = trial.execute_sql(req.query, req.params)
            return {"results": results, "count": len(results)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"SQL query failed: {str(e)}")


@app.get("/schema")
async def get_schema_legacy() -> dict[str, Any]:
    """Get database schema information (legacy endpoint)."""
    trial = get_or_create_default_trial()

    with trial._lock:
        if not trial._sql_conn:
            return {"tables": []}

        cursor = trial._sql_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = cursor.fetchall()

        schema = {}
        for (table_name,) in tables:
            cursor.execute(f'PRAGMA table_info("{table_name}")')
            columns = cursor.fetchall()
            schema[table_name] = [
                {"name": col[1], "type": col[2], "notnull": bool(col[3]), "pk": bool(col[5])}
                for col in columns
            ]

        return {"tables": schema}
