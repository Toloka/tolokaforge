"""
DB Service HTTP Client for Runner

This module provides an async HTTP client for communicating with the DB Service.
Each method maps 1:1 to a DB Service endpoint as defined in docs/DB_SERVICE_API.md.

Usage:
    client = DBServiceClient("http://db-service:8000")
    await client.init_trial("task:0", tables={"users": []}, schemas=[], unstable_fields=[])
    state = await client.get_state("task:0")
"""

import logging
from typing import Any

import httpx

from tolokaforge.runner.models import (
    DeleteTrialResponse,
    HashResponse,
    HealthCheckResponse,
    InitTrialResponse,
    MutateResponse,
    QueryResponse,
    ResetTrialResponse,
    RestoreSnapshotResponse,
    SchemaResponse,
    SnapshotResponse,
    StableStateResponse,
    StateResponse,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Custom Exceptions
# =============================================================================


class DBServiceError(Exception):
    """Base exception for DB Service errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


class TrialNotFoundError(DBServiceError):
    """Trial not found in DB Service."""

    def __init__(self, trial_id: str):
        self.trial_id = trial_id
        super().__init__(f"Trial '{trial_id}' not found", {"trial_id": trial_id})


class TrialAlreadyExistsError(DBServiceError):
    """Trial already exists in DB Service."""

    def __init__(self, trial_id: str):
        self.trial_id = trial_id
        super().__init__(f"Trial '{trial_id}' already exists", {"trial_id": trial_id})


class TableNotFoundError(DBServiceError):
    """Table not found in trial state."""

    def __init__(self, table_name: str):
        self.table_name = table_name
        super().__init__(f"Table '{table_name}' not found", {"table_name": table_name})


class SnapshotNotFoundError(DBServiceError):
    """Snapshot not found for trial."""

    def __init__(self, snapshot_name: str):
        self.snapshot_name = snapshot_name
        super().__init__(f"Snapshot '{snapshot_name}' not found", {"snapshot_name": snapshot_name})


class SnapshotAlreadyExistsError(DBServiceError):
    """Snapshot already exists for trial."""

    def __init__(self, snapshot_name: str):
        self.snapshot_name = snapshot_name
        super().__init__(
            f"Snapshot '{snapshot_name}' already exists", {"snapshot_name": snapshot_name}
        )


class InvalidOperationError(DBServiceError):
    """Invalid mutation operation."""

    def __init__(self, message: str, operation: str | None = None):
        super().__init__(message, {"operation": operation} if operation else {})


class ETagMismatchError(DBServiceError):
    """ETag mismatch during optimistic locking."""

    def __init__(self, expected_etag: str):
        self.expected_etag = expected_etag
        super().__init__("State was modified (ETag mismatch)", {"expected_etag": expected_etag})


class ValidationError(DBServiceError):
    """Request validation failed."""

    pass


class ConnectionError(DBServiceError):
    """Failed to connect to DB Service."""

    pass


# =============================================================================
# Test Client Context Manager (for in-process testing)
# =============================================================================


class _TestClientContextManager:
    """Context manager wrapper for test clients that don't need cleanup.

    This allows injected test clients (like MockAsyncClient wrapping FastAPI TestClient)
    to be used with `async with` statements without requiring actual cleanup.
    """

    def __init__(self, client: Any):
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


# =============================================================================
# DB Service Client
# =============================================================================


class DBServiceClient:
    """
    Async HTTP client for the DB Service.

    Provides methods that map 1:1 to DB Service endpoints as defined in
    docs/DB_SERVICE_API.md. All methods return typed Pydantic response models.

    Example:
        client = DBServiceClient("http://db-service:8000")

        # Initialize a trial
        response = await client.init_trial(
            trial_id="airline_task_001:0",
            tables={"users": [{"id": "u1", "name": "Alice"}]},
            schemas=[{"table_name": "users", "fields": {"id": "string", "name": "string"}}],
            unstable_fields=[{"table_name": "users", "field_name": "id", "reason": "auto_id"}]
        )

        # Get state
        state = await client.get_state("airline_task_001:0")

        # Mutate state
        await client.mutate(
            trial_id="airline_task_001:0",
            table_name="users",
            operations=[{"op": "insert", "record": {"id": "u2", "name": "Bob"}}]
        )

    For testing with FastAPI TestClient, inject a mock client:
        from fastapi.testclient import TestClient
        test_client = TestClient(app)
        db_client = DBServiceClient("http://testserver")
        db_client.set_test_client(MockAsyncClient(test_client, "http://testserver"))
    """

    def __init__(self, base_url: str, timeout: float = 30.0):
        """
        Initialize the DB Service client.

        Args:
            base_url: Base URL of the DB Service (e.g., "http://db-service:8000")
            timeout: Default timeout for HTTP requests in seconds

        Note: This client creates a new httpx.AsyncClient per request to handle
        gRPC's threading model where each RPC handler runs in a different thread.
        This avoids event loop issues when the loop closes between gRPC calls.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Optional injected test client for in-process testing
        self._test_client: Any | None = None
        # Note: We intentionally do NOT share an AsyncClient across requests
        # because gRPC runs each RPC handler in a ThreadPoolExecutor thread,
        # and the event loop may close between calls. Creating a fresh client
        # per request avoids "Event loop is closed" errors.

    def set_test_client(self, client: Any) -> None:
        """
        Inject a test client for in-process testing.

        This allows tests to use a MockAsyncClient wrapping FastAPI's TestClient
        instead of making real HTTP requests.

        Args:
            client: A mock async client that implements get/post/put/delete/patch methods
        """
        self._test_client = client

    def _create_client(self) -> Any:
        """Create or return an HTTP client for a request.

        If a test client has been injected, returns a context manager wrapper
        around it. Otherwise, creates a fresh httpx.AsyncClient per request
        to avoid event loop issues in gRPC's ThreadPoolExecutor threads.
        """
        if self._test_client is not None:
            # Return a context manager wrapper for the test client
            return _TestClientContextManager(self._test_client)
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
        )

    async def close(self) -> None:
        """Close the HTTP client (no-op since we create per-request clients)."""
        pass

    async def __aenter__(self) -> "DBServiceClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        pass

    def _handle_error_response(self, response: httpx.Response) -> None:
        """
        Handle error responses from DB Service.

        Raises appropriate typed exceptions based on the error response.
        FAIL FAST: All errors are raised, never swallowed.
        """
        if response.status_code == 200 or response.status_code == 201:
            return

        # Parse error response
        error_type = ""
        message = ""
        details: dict[str, Any] = {}

        try:
            error_data = response.json()
            if isinstance(error_data, dict) and "detail" in error_data:
                detail = error_data["detail"]
                if isinstance(detail, dict):
                    error_type = detail.get("error", "")
                    message = detail.get("message", str(detail))
                    details = detail.get("details", {})
                else:
                    message = str(detail)
            else:
                message = str(error_data)
        except ValueError:
            # JSON parsing failed - use raw text
            message = response.text or f"HTTP {response.status_code}"

        # Map error types to exceptions (FAIL FAST - always raise)
        if response.status_code == 404:
            if error_type == "TrialNotFound":
                raise TrialNotFoundError(details.get("trial_id", "unknown"))
            elif error_type == "TableNotFound":
                raise TableNotFoundError(details.get("table_name", "unknown"))
            elif error_type == "SnapshotNotFound":
                raise SnapshotNotFoundError(details.get("snapshot_name", "unknown"))
            else:
                raise DBServiceError(message, details)

        elif response.status_code == 409:
            if error_type == "TrialAlreadyExists":
                raise TrialAlreadyExistsError(details.get("trial_id", "unknown"))
            elif error_type == "SnapshotAlreadyExists":
                raise SnapshotAlreadyExistsError(details.get("snapshot_name", "unknown"))
            elif error_type == "ETagMismatch":
                raise ETagMismatchError(details.get("expected_etag", "unknown"))
            else:
                raise DBServiceError(message, details)

        elif response.status_code == 400:
            if error_type == "InvalidOperation":
                raise InvalidOperationError(message, details.get("operation"))
            else:
                raise ValidationError(message, details)

        else:
            raise DBServiceError(f"HTTP {response.status_code}: {message}", details)

    # =========================================================================
    # Trial Lifecycle Endpoints
    # =========================================================================

    async def init_trial(
        self,
        trial_id: str,
        tables: dict[str, list[dict[str, Any]]],
        schemas: list[dict[str, Any]] | None = None,
        unstable_fields: list[dict[str, Any]] | None = None,
    ) -> InitTrialResponse:
        """
        Initialize a trial with initial state, schemas, and unstable field specifications.

        Maps to: POST /trials/{trial_id}/init

        Args:
            trial_id: Unique trial identifier (e.g., "airline_task_001:0")
            tables: Initial data as table_name -> list of records
            schemas: Optional list of TableSchema definitions
            unstable_fields: Optional list of UnstableFieldSpec definitions

        Returns:
            InitTrialResponse with status, trial_id, tables_initialized, etc.

        Raises:
            TrialAlreadyExistsError: If trial already exists
            ValidationError: If request body is invalid
            ConnectionError: If cannot connect to DB Service
        """
        payload: dict[str, Any] = {"tables": tables}
        if schemas is not None:
            payload["schemas"] = schemas
        if unstable_fields is not None:
            payload["unstable_fields"] = unstable_fields

        async with self._create_client() as client:
            try:
                response = await client.post(f"/trials/{trial_id}/init", json=payload)
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return InitTrialResponse.model_validate(response.json())

    async def get_state(
        self,
        trial_id: str,
        tables: list[str] | None = None,
    ) -> StateResponse:
        """
        Get the complete current state including all fields.

        Maps to: GET /trials/{trial_id}/state

        Args:
            trial_id: Trial identifier
            tables: Optional list of table names to return (default: all)

        Returns:
            StateResponse with data, version, full_hash, stable_hash

        Raises:
            TrialNotFoundError: If trial not found
        """
        params = {}
        if tables:
            params["tables"] = ",".join(tables)

        async with self._create_client() as client:
            try:
                response = await client.get(f"/trials/{trial_id}/state", params=params)
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return StateResponse.model_validate(response.json())

    async def get_stable_state(self, trial_id: str) -> StableStateResponse:
        """
        Get state with unstable fields filtered out.

        Maps to: GET /trials/{trial_id}/state/stable

        Args:
            trial_id: Trial identifier

        Returns:
            StableStateResponse with data (filtered), version, stable_hash, filtered_fields

        Raises:
            TrialNotFoundError: If trial not found
        """
        async with self._create_client() as client:
            try:
                response = await client.get(f"/trials/{trial_id}/state/stable")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return StableStateResponse.model_validate(response.json())

    async def get_stable_hash(self, trial_id: str) -> str:
        """
        Get SHA-256 hash of the stable state.

        Maps to: GET /trials/{trial_id}/state/hash

        Args:
            trial_id: Trial identifier

        Returns:
            The stable_hash string

        Raises:
            TrialNotFoundError: If trial not found
        """
        async with self._create_client() as client:
            try:
                response = await client.get(f"/trials/{trial_id}/state/hash")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            hash_response = HashResponse.model_validate(response.json())
            return hash_response.stable_hash

    async def mutate(
        self,
        trial_id: str,
        table_name: str,
        operations: list[dict[str, Any]],
        etag: str | None = None,
    ) -> MutateResponse:
        """
        Apply mutations to a specific table.

        Maps to: PATCH /trials/{trial_id}/state/{table_name}

        Args:
            trial_id: Trial identifier
            table_name: Name of the table to mutate
            operations: List of mutation operations (insert, update, delete, upsert)
            etag: Optional ETag for optimistic locking

        Returns:
            MutateResponse with status, version, affected_rows, new_hash

        Raises:
            TrialNotFoundError: If trial not found
            TableNotFoundError: If table not found
            InvalidOperationError: If operation is invalid
            ETagMismatchError: If ETag doesn't match
        """
        payload: dict[str, Any] = {"operations": operations}
        if etag is not None:
            payload["etag"] = etag

        async with self._create_client() as client:
            try:
                response = await client.patch(
                    f"/trials/{trial_id}/state/{table_name}",
                    json=payload,
                )
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return MutateResponse.model_validate(response.json())

    # =========================================================================
    # Snapshot Endpoints
    # =========================================================================

    async def create_snapshot(self, trial_id: str, name: str) -> SnapshotResponse:
        """
        Create a named snapshot of the current state.

        Maps to: POST /trials/{trial_id}/snapshots/{snapshot_name}

        Args:
            trial_id: Trial identifier
            name: Snapshot name

        Returns:
            SnapshotResponse with status, snapshot_name, version, hash

        Raises:
            TrialNotFoundError: If trial not found
            SnapshotAlreadyExistsError: If snapshot name already exists
        """
        async with self._create_client() as client:
            try:
                response = await client.post(f"/trials/{trial_id}/snapshots/{name}")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return SnapshotResponse.model_validate(response.json())

    async def restore_snapshot(self, trial_id: str, name: str) -> RestoreSnapshotResponse:
        """
        Restore state from a named snapshot.

        Maps to: POST /trials/{trial_id}/snapshots/{snapshot_name}/restore

        Args:
            trial_id: Trial identifier
            name: Snapshot name to restore

        Returns:
            RestoreSnapshotResponse with status, restored_from, version, hash

        Raises:
            TrialNotFoundError: If trial not found
            SnapshotNotFoundError: If snapshot not found
        """
        async with self._create_client() as client:
            try:
                response = await client.post(f"/trials/{trial_id}/snapshots/{name}/restore")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return RestoreSnapshotResponse.model_validate(response.json())

    # =========================================================================
    # Reset and Delete Endpoints
    # =========================================================================

    async def reset_trial(self, trial_id: str) -> ResetTrialResponse:
        """
        Reset trial state to the initial state provided during init.

        Maps to: POST /trials/{trial_id}/reset

        Args:
            trial_id: Trial identifier

        Returns:
            ResetTrialResponse with status, version, hash

        Raises:
            TrialNotFoundError: If trial not found
        """
        async with self._create_client() as client:
            try:
                response = await client.post(f"/trials/{trial_id}/reset")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return ResetTrialResponse.model_validate(response.json())

    async def delete_trial(self, trial_id: str) -> DeleteTrialResponse:
        """
        Clean up all data for a trial.

        Maps to: DELETE /trials/{trial_id}

        Args:
            trial_id: Trial identifier

        Returns:
            DeleteTrialResponse with status and deleted info

        Raises:
            TrialNotFoundError: If trial not found
        """
        async with self._create_client() as client:
            try:
                response = await client.delete(f"/trials/{trial_id}")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return DeleteTrialResponse.model_validate(response.json())

    # =========================================================================
    # Query Endpoints
    # =========================================================================

    async def query(self, trial_id: str, jsonpath: str) -> QueryResponse:
        """
        Query state using JSONPath expressions.

        Maps to: POST /trials/{trial_id}/query

        Args:
            trial_id: Trial identifier
            jsonpath: JSONPath expression

        Returns:
            QueryResponse with results and count

        Raises:
            TrialNotFoundError: If trial not found
            ValidationError: If query is invalid
        """
        async with self._create_client() as client:
            try:
                response = await client.post(
                    f"/trials/{trial_id}/query",
                    json={"jsonpath": jsonpath},
                )
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return QueryResponse.model_validate(response.json())

    async def sql_query(
        self,
        trial_id: str,
        query: str,
        params: list[Any] | None = None,
    ) -> QueryResponse:
        """
        Execute SQL query on the trial state.

        Maps to: POST /trials/{trial_id}/sql

        Args:
            trial_id: Trial identifier
            query: SQL query string
            params: Optional query parameters

        Returns:
            QueryResponse with results and count

        Raises:
            TrialNotFoundError: If trial not found
            ValidationError: If query is invalid
        """
        payload: dict[str, Any] = {"query": query}
        if params is not None:
            payload["params"] = params

        async with self._create_client() as client:
            try:
                response = await client.post(f"/trials/{trial_id}/sql", json=payload)
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return QueryResponse.model_validate(response.json())

    # =========================================================================
    # Schema and Health Endpoints
    # =========================================================================

    async def get_schema(self, trial_id: str) -> SchemaResponse:
        """
        Get registered schemas and unstable field specifications.

        Maps to: GET /trials/{trial_id}/schema

        Args:
            trial_id: Trial identifier

        Returns:
            SchemaResponse with schemas and unstable_fields

        Raises:
            TrialNotFoundError: If trial not found
        """
        async with self._create_client() as client:
            try:
                response = await client.get(f"/trials/{trial_id}/schema")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return SchemaResponse.model_validate(response.json())

    async def health_check(self) -> HealthCheckResponse:
        """
        Service health check (not trial-specific).

        Maps to: GET /health

        Returns:
            HealthCheckResponse with status, version, active_trials

        Raises:
            ConnectionError: If cannot connect to DB Service
        """
        async with self._create_client() as client:
            try:
                response = await client.get("/health")
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to DB Service: {e}")

            self._handle_error_response(response)
            return HealthCheckResponse.model_validate(response.json())
