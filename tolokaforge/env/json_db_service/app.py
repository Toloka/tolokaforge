"""JSON DB service - REST API for versioned JSON state with SQL support and trial isolation.

This module provides a schema-aware JSON state storage with:
- Trial isolation: Each trial has isolated state, schemas, and snapshots
- Unstable field filtering: Explicit field exclusion for deterministic hashing
- Snapshot/Restore: Supports golden path execution during grading
- SQL queries: SQLite-based querying on JSON data
- JSONPath queries: Query state using JSONPath expressions

Deployment. This service ships as a container built from
:file:`tolokaforge/docker/dockerfiles/db_service.Dockerfile`, which installs
`fastapi` / `uvicorn` / `jsonpath-ng` from :file:`requirements.txt` next to
this file. The core `tolokaforge` wheel does NOT depend on `fastapi`; a
plain `pip install tolokaforge` will not import this module. Callers that
run the service outside the container (typically the test suite, which
uses `TestClient`) need `pip install 'tolokaforge[runner]'` — the `runner`
extra carries `fastapi>=0.108.0`.
"""

import copy
import hashlib
import json
import logging
import re
import reprlib
import sqlite3
from collections.abc import Iterable
from threading import Lock
from typing import Any, NoReturn

try:
    from fastapi import FastAPI, HTTPException, Query
except ImportError as exc:  # noqa: BLE001 -- explicit re-raise below
    raise ImportError(
        "tolokaforge.env.json_db_service.app requires `fastapi`. "
        "The service ships as a container that installs its own dependencies "
        "(tolokaforge/env/json_db_service/requirements.txt). To run this "
        "module outside the container — for example the test suite — install "
        "the runner extra: `pip install 'tolokaforge[runner]'`."
    ) from exc

from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext import parse  # .ext: supports filter exprs, superset of base grammar
from jsonpath_ng.ext.string import DefintionInvalid
from jsonpath_ng.jsonpath import Child, Fields, JSONPath
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
        *,
        canonicalize_numbers: bool = True,
        numeric_string_fields: list[str] | None = None,
    ) -> str:
        """Compute a stable SHA-256 hash of the state dictionary.

        Standalone fallback — mirrors tolokaforge.core.hash.compute_stable_hash,
        including the two-tier numeric canonicalization: numeric TYPES always
        fold; numeric-looking STRINGS fold only under a record key listed in
        ``numeric_string_fields``. Keep the two in sync.
        """
        from decimal import Decimal, InvalidOperation

        string_fields = frozenset(numeric_string_fields) if numeric_string_fields else None

        def _canon(v: Any, normalize_strings: bool) -> Any:
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float, Decimal)):
                try:
                    d = Decimal(str(v)).normalize()
                    return "\x00tf-num:" + format(abs(d) if d.is_zero() else d, "f")
                except (InvalidOperation, ValueError):
                    return v
            if isinstance(v, str):
                if normalize_strings:
                    s = v.strip()
                    body = s[1:] if s[:1] in ("+", "-") else s
                    ip, dot, fp = body.partition(".")
                    if dot:
                        numeric = (
                            fp.isascii()
                            and fp.isdigit()
                            and (not ip or (ip.isascii() and ip.isdigit()))
                        )
                    else:
                        numeric = ip.isascii() and ip.isdigit()
                    numeric = numeric and not (len(ip) > 1 and ip[0] == "0")
                    if 0 < len(s) <= 64 and numeric:
                        try:
                            d = Decimal(s).normalize()
                            return "\x00tf-num:" + format(abs(d) if d.is_zero() else d, "f")
                        except InvalidOperation:
                            pass
                if v.startswith("\x00"):
                    return "\x00tf-esc:" + v
            return v

        def _walk(data: Any, normalize_strings: bool = False) -> Any:
            if isinstance(data, dict):
                return {
                    k: _walk(x, string_fields is not None and k in string_fields)
                    for k, x in data.items()
                }
            if isinstance(data, list):
                return [_walk(x, normalize_strings) for x in data]
            if isinstance(data, tuple):
                return tuple(_walk(x, normalize_strings) for x in data)
            return _canon(data, normalize_strings)

        if unstable_fields:
            state = filter_unstable_fields(state, unstable_fields)

        serializable_state = _convert_datetime_to_str(state)
        if canonicalize_numbers:
            serializable_state = _walk(serializable_state)
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
    key: str | list[str] | None = None  # for upsert; a list is an ordered composite key


class MutationRequest(BaseModel):
    """Mutation request with operations"""

    operations: list[MutationOperation]
    etag: str | None = None


class JSONPathOp(BaseModel):
    """One ``add`` / ``replace`` / ``remove`` op addressed by a JSONPath."""

    op: str
    path: str
    value: Any = None

    model_config = {"extra": "forbid"}


class TrialUpdateRequest(BaseModel):
    """A batch of JSONPath ops applied to one trial as a unit."""

    ops: list[JSONPathOp]

    model_config = {"extra": "forbid"}


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

    def sync_json_to_sql(self) -> None:
        """Rebuild the SQL mirror from ``data``, one table per top-level key.

        A table's columns are the union of every row's keys, so rows with
        differing fields mirror into one table. A table with no rows has no SQL
        table.

        Raises:
            SQLMirrorError: If a table, key or value cannot be stored in SQLite.
                The mirror is then partly rebuilt; re-sync from a storable state.
        """
        if not self._sql_conn:
            self._init_sql_db()

        assert self._sql_conn is not None  # For type checker
        cursor = self._sql_conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for (existing,) in cursor.fetchall():
            cursor.execute(f"DROP TABLE IF EXISTS {_sql_identifier(existing)}")

        for table_name, table_data in self.data.items():
            self._mirror_table(cursor, table_name, table_data)
        self._sql_conn.commit()

    def _mirror_table(self, cursor: sqlite3.Cursor, table_name: str, rows: Any) -> None:
        if not isinstance(rows, list):
            return
        records = [row for row in rows if isinstance(row, dict)]
        all_columns = self._infer_columns(records)
        if not all_columns:
            return
        table = _sql_identifier(table_name)
        columns = ", ".join(f"{_sql_identifier(key)} {kind}" for key, kind in all_columns.items())
        try:
            cursor.execute(f"CREATE TABLE {table} ({columns})")
        except (sqlite3.Error, UnicodeEncodeError) as e:
            raise SQLMirrorError(table_name, _uncreatable_table_reason(all_columns, e)) from e
        column_names = list(all_columns)
        keys = ", ".join(_sql_identifier(key) for key in column_names)
        placeholders = ", ".join("?" for _ in column_names)
        insert_sql = f"INSERT INTO {table} ({keys}) VALUES ({placeholders})"
        for row_index, record in enumerate(records):
            values = [self._serialize_for_sql(record.get(col)) for col in column_names]
            try:
                cursor.execute(insert_sql, values)
            except (sqlite3.Error, OverflowError, UnicodeEncodeError) as e:
                raise SQLMirrorError(
                    table_name, _unstorable_row_reason(cursor, row_index, column_names, values, e)
                ) from e

    def _infer_columns(self, records: list[dict[str, Any]]) -> dict[str, str]:
        """Column name to SQL type over every row; a ``TEXT`` column takes a later non-null type."""
        columns: dict[str, str] = {}
        for key, value in (item for record in records for item in record.items()):
            if key not in columns or (columns[key] == "TEXT" and value is not None):
                columns[key] = self._infer_sql_type(value)
        return columns

    def _serialize_for_sql(self, value: Any) -> Any:
        """A value SQLite can bind: a list or dict as JSON text, a bool as an int."""
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

    def compute_stable_hash(self, *, numeric_string_fields: list[str] | None = None) -> str:
        """Compute hash of stable state (unstable fields filtered)."""
        unstable_list = self.get_unstable_field_list()
        return compute_stable_hash(
            self.data, unstable_list, numeric_string_fields=numeric_string_fields
        )

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


class SQLMirrorError(ValueError):
    """The JSON state holds a table, key or value the SQLite mirror cannot store."""

    def __init__(self, table: str, reason: str):
        self.table = table
        super().__init__(f"table '{table}' {reason}")


def _sql_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _uncreatable_table_reason(columns: Iterable[str], error: Exception) -> str:
    """Name the first row key SQLite cannot take as a column name, else the table itself."""
    for column in columns:
        try:
            column.encode("utf-8")
        except UnicodeEncodeError:
            return f"key {reprlib.repr(column)} cannot name a SQL column ({error})"
    return f"cannot be created in SQL ({error})"


def _unstorable_row_reason(
    cursor: sqlite3.Cursor,
    row_index: int,
    columns: list[str],
    values: list[Any],
    error: Exception,
) -> str:
    """Name the first field of a refused row SQLite cannot bind, else the row itself."""
    for column, value in zip(columns, values, strict=True):
        try:
            cursor.execute("SELECT ?", (value,))
        except (sqlite3.Error, OverflowError, UnicodeEncodeError):
            return (
                f"row {row_index} field '{column}' holds {reprlib.repr(value)}, "
                f"which SQLite cannot store ({error})"
            )
    return f"row {row_index} cannot be stored in SQL ({error})"


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


def _refuse_missing_key_fields(
    table_name: str, missing: list[str], record: dict[str, Any], op_index: int, reason: str
) -> NoReturn:
    record_keys = sorted(record)
    raise HTTPException(
        status_code=400,
        detail=error_response(
            "InvalidOperation",
            f"Upsert record for table '{table_name}' {reason} "
            f"(operation {op_index}); record keys: {record_keys}",
            {
                "table_name": table_name,
                "missing_components": missing,
                "record_keys": record_keys,
                "op_index": op_index,
            },
        ),
    )


def upsert_key_fields(
    key: str | list[str] | None, record: dict[str, Any], table_name: str, op_index: int
) -> list[str]:
    """Resolve an upsert's ``key`` to the field names the matcher compares.

    A string (or omitted, defaulting to ``id``) key resolves to that single
    field, which the record must contain — an explicit ``None`` value is a
    legal, addressable key value. A composite key must name at least one
    field, and every component must carry a non-null value in the record —
    a ``None`` component cannot address a row.
    """
    if not isinstance(key, list):
        field = key or "id"
        if field not in record:
            _refuse_missing_key_fields(
                table_name,
                [field],
                record,
                op_index,
                f"does not contain key field '{field}'",
            )
        return [field]
    if not key:
        raise HTTPException(
            status_code=400,
            detail=error_response(
                "InvalidOperation",
                f"Upsert 'key' list for table '{table_name}' must name at least "
                f"one field (operation {op_index})",
                {"table_name": table_name, "op_index": op_index},
            ),
        )
    missing = [field for field in key if record.get(field) is None]
    if missing:
        _refuse_missing_key_fields(
            table_name,
            missing,
            record,
            op_index,
            f"is missing key component(s) {missing} (a null-valued component cannot address a row)",
        )
    return key


def validate_upsert_operations(
    operations: list[MutationOperation], table_name: str
) -> dict[int, list[str]]:
    """Resolve and validate every upsert's key before any operation applies.

    A refused batch mutates nothing: rows, version, and the SQL mirror stay
    untouched, and no table is auto-created.
    """
    key_fields_by_op: dict[int, list[str]] = {}
    for op_index, op in enumerate(operations):
        if op.op != "upsert":
            continue
        if op.record is None:
            raise HTTPException(
                status_code=400,
                detail=error_response(
                    "InvalidOperation",
                    "Upsert requires 'record'",
                    {"op": op.op, "op_index": op_index},
                ),
            )
        key_fields_by_op[op_index] = upsert_key_fields(op.key, op.record, table_name, op_index)
    return key_fields_by_op


_JSONPATH_EXAMPLE = "$.tickets[0].status"


def _find_jsonpath(path: str, data: Any, op_index: int | None = None) -> list[Any]:
    """Match ``path`` against ``data``, refusing a parse or evaluation fault with 400."""
    return _match_jsonpath(_parse_jsonpath(path, path, op_index), data, path, op_index)


def _parse_jsonpath(path: str, shown: str, op_index: int | None) -> JSONPath:
    """Parse ``path``, refusing with 400 a path that does not parse; a refusal names ``shown``."""
    try:
        return parse(path)
    except JSONPathError as e:
        _refuse_jsonpath(shown, f"is not a valid JSONPath ({e})", op_index)


def _match_jsonpath(expr: JSONPath, data: Any, shown: str, op_index: int | None) -> list[Any]:
    """Match ``expr`` against ``data``, refusing with 400 a fault ``shown`` causes at match time.

    A filter's regex and an ``.ext`` string function are compiled lazily, so a
    bad one surfaces here rather than at parse time.
    """
    try:
        return expr.find(data)
    except (re.error, DefintionInvalid) as e:
        _refuse_jsonpath(shown, f"cannot be evaluated ({e})", op_index)


def _refuse_jsonpath(path: str, reason: str, op_index: int | None) -> NoReturn:
    where = "" if op_index is None else f"op {op_index}: "
    details: dict[str, Any] = {"path": path}
    if op_index is not None:
        details["op_index"] = op_index
    raise HTTPException(
        status_code=400,
        detail=error_response(
            "InvalidJSONPath",
            f"{where}path '{path}' {reason}; paths are JSONPath, e.g. '{_JSONPATH_EXAMPLE}'",
            details,
        ),
    )


def _refuse_op(op_index: int, op: JSONPathOp, reason: str) -> NoReturn:
    raise HTTPException(
        status_code=400,
        detail=error_response(
            "InvalidOperation",
            f"op {op_index}: {reason}",
            {"op_index": op_index, "op": op.op, "path": op.path},
        ),
    )


_ADD_APPEND_SUFFIX = ".-"
_ADD_APPEND_EXAMPLE = f"$.tickets{_ADD_APPEND_SUFFIX}"


def _matches_below_root(data: dict[str, Any], op: JSONPathOp, op_index: int) -> list[Any]:
    """``op.path``'s matches, refusing a match on the root ``$`` itself."""
    matches = _find_jsonpath(op.path, data, op_index)
    if any(match.context is None for match in matches):
        _refuse_op(
            op_index,
            op,
            f"{op.op} path '{op.path}' addresses the root '$'; "
            "address a table or a row in one, e.g. '$.tickets[0]'",
        )
    return matches


def _replace_at(data: dict[str, Any], op: JSONPathOp, op_index: int) -> None:
    matches = _matches_below_root(data, op, op_index)
    if not matches:
        _refuse_op(op_index, op, f"replace path '{op.path}' matches nothing")
    for match in matches:
        match.full_path.update(data, op.value)


def _add_target(op: JSONPathOp, op_index: int) -> tuple[JSONPath, str]:
    """The parent expression of an add path and the one key it ends in, as JSONPath parses it.

    A path ending in ``.-`` is the append form, which JSONPath has no syntax for.
    """
    if op.path.endswith(_ADD_APPEND_SUFFIX):
        parent_path = op.path.removesuffix(_ADD_APPEND_SUFFIX)
        return _parse_jsonpath(parent_path, op.path, op_index), "-"
    expr = _parse_jsonpath(op.path, op.path, op_index)
    if isinstance(expr, Child) and isinstance(expr.right, Fields):
        fields = expr.right.fields
        if len(fields) == 1 and fields[0] != "*":
            return expr.left, fields[0]
    _refuse_op(
        op_index,
        op,
        f"add path '{op.path}' does not end in a key name: add sets a named key on "
        f"the object its parent path matches, or appends to a list parent, "
        f"e.g. '{_ADD_APPEND_EXAMPLE}'",
    )


def _add_at(data: dict[str, Any], op: JSONPathOp, op_index: int) -> None:
    parent_expr, key = _add_target(op, op_index)
    parents = _match_jsonpath(parent_expr, data, op.path, op_index)
    if not parents:
        _refuse_op(op_index, op, f"add path '{op.path}' has a parent that matches nothing")
    for parent in parents:
        if isinstance(parent.value, dict):
            parent.value[key] = op.value
        elif isinstance(parent.value, list):
            parent.value.append(op.value)
        else:
            _refuse_op(
                op_index,
                op,
                f"add path '{op.path}' has a parent holding a "
                f"{type(parent.value).__name__}; add needs an object or a list there",
            )


def _remove_at(data: dict[str, Any], op: JSONPathOp, op_index: int) -> None:
    for match in _matches_below_root(data, op, op_index):
        parent = match.context.value
        if isinstance(parent, dict) and match.path.fields:
            del parent[match.path.fields[0]]
        elif isinstance(parent, list):
            parent.remove(match.value)


_JSONPATH_OPS = {"replace": _replace_at, "add": _add_at, "remove": _remove_at}


def _apply_jsonpath_op(data: dict[str, Any], op: JSONPathOp, op_index: int) -> None:
    """Apply one op to ``data`` in place, refusing a client-caused fault with 400.

    ``replace`` refuses a path that matches nothing. ``add`` takes the key its
    path ends in as JSONPath parses it, which must be a single key name, or a
    trailing ``.-``; every match of the parent must be a dict, which gains that
    key, or a list, which has the value appended; a parent matching nothing is
    refused. ``replace`` and ``remove`` refuse the root ``$``. ``remove`` of a
    path that matches nothing is a no-op.
    """
    apply = _JSONPATH_OPS.get(op.op)
    if apply is None:
        _refuse_op(op_index, op, f"unknown op '{op.op}'; expected add, replace or remove")
    if not op.path.startswith("$"):
        _refuse_jsonpath(op.path, "is not a JSONPath", op_index)
    apply(data, op, op_index)


def _refuse_non_table_shape(data: dict[str, Any], op: JSONPathOp, op_index: int) -> None:
    """Refuse a state that is no longer a map of table name to a list of row objects."""
    for table, rows in data.items():
        if not isinstance(rows, list):
            _refuse_op(
                op_index,
                op,
                f"would leave table '{table}' as {type(rows).__name__}; "
                "every top-level key must hold a list of row objects",
            )
        bad_row = next((row for row in rows if not isinstance(row, dict)), None)
        if bad_row is not None:
            _refuse_op(
                op_index,
                op,
                f"would put a {type(bad_row).__name__} row in table '{table}'; "
                "every top-level key must hold a list of row objects",
            )


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
async def get_state_hash(
    trial_id: str,
    numeric_string_fields: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Get SHA-256 hash of the stable state.

    ``numeric_string_fields`` is the opt-in per-field grading list: record
    field names whose numeric-looking string values fold ("130.00" == "130.0")
    — see core/hash.py compute_stable_hash.
    """
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        return {
            "stable_hash": trial.compute_stable_hash(numeric_string_fields=numeric_string_fields),
            "full_hash": trial.compute_full_hash(),
            "version": trial.version,
        }


@app.patch("/trials/{trial_id}/state/{table_name}")
async def mutate_state(trial_id: str, table_name: str, req: MutationRequest) -> dict[str, Any]:
    """Apply mutations to a specific table.

    Every upsert's key is validated before any operation applies, so a batch
    refused over an upsert mutates nothing.

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
        # A missing table is auto-created only when the first operation could
        # seed it (insert/upsert), and only after the batch passed validation.
        table_missing = table_name not in trial.data
        if table_missing and not (req.operations and req.operations[0].op in ("insert", "upsert")):
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

        upsert_key_fields_by_op = validate_upsert_operations(req.operations, table_name)

        if table_missing:
            logger.info(
                "Auto-creating table for insert/upsert operation",
                extra={"trial_id": trial_id, "table_name": table_name},
            )
            trial.data[table_name] = []

        affected_rows = 0
        table_data = trial.data[table_name]

        for op_index, op in enumerate(req.operations):
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
                key_fields = upsert_key_fields_by_op[op_index]
                found = False
                for record in table_data:
                    if all(f in record and record[f] == op.record[f] for f in key_fields):
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
        results = [match.value for match in _find_jsonpath(req.jsonpath, trial.data)]
        return {"results": results, "count": len(results)}


def _commit_mirrored(trial: TrialState, working: dict[str, Any]) -> None:
    """Make ``working`` the trial's state, or refuse with 400 if the SQL mirror cannot store it.

    Whatever stops the mirror, the previous state is restored and re-mirrored before
    the error propagates, so the trial is left exactly as it was.
    """
    previous = trial.data
    trial.data = working
    try:
        trial.sync_json_to_sql()
    except Exception as e:
        trial.data = previous
        trial.sync_json_to_sql()
        if not isinstance(e, SQLMirrorError):
            raise
        raise HTTPException(
            status_code=400,
            detail=error_response(
                "InvalidOperation",
                f"the batch would leave a state the SQL mirror cannot store: {e}; "
                "nothing was applied",
                {"table": e.table},
            ),
        ) from e


@app.post("/trials/{trial_id}/update")
async def update_trial(trial_id: str, req: TrialUpdateRequest) -> dict[str, Any]:
    """Apply a batch of JSONPath ops to the trial state, all or nothing.

    The ops run in order on a copy of the state, which is committed only if
    every op succeeds, every top-level key still holds a list of row objects,
    and the SQL mirror can store the result. A refused batch leaves rows,
    version and the SQL mirror as they were.
    """
    try:
        trial = db_service.get_trial(trial_id)
    except TrialNotFoundError as e:
        handle_trial_not_found(e)

    with trial._lock:
        working = copy.deepcopy(trial.data)
        for op_index, op in enumerate(req.ops):
            _apply_jsonpath_op(working, op, op_index)
            _refuse_non_table_shape(working, op, op_index)
        _commit_mirrored(trial, working)
        trial.version += 1
        return {
            "status": "ok",
            "version": trial.version,
            "stable_hash": trial.compute_stable_hash(),
        }


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
