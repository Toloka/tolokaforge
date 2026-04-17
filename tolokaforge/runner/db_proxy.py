"""
DB Service Proxy for MCP Async Tools

This module provides a proxy class that mimics the InMemoryDatabase interface
but translates method calls to DB Service HTTP requests. This allows TlkMcpCore
MCP tools to work with the DB Service without modification.

The proxy implements the key methods from InMemoryDatabase:
- get_all(model_cls) -> List[T]
- get_by_id(model_cls, id) -> Optional[T]
- create(obj) -> T
- update(obj) -> T
- delete(obj) -> None
- delete_by_id(model_cls, id) -> None

Since MCP tools use Pydantic models, the proxy handles conversion between
Pydantic models and dict representations for HTTP transport.

Usage:
    db_client = DBServiceClient("http://db-service:8000")
    proxy = DBServiceProxy(db_client, "trial_id:0")

    # Use like InMemoryDatabase
    users = await proxy.get_all(User)
    user = await proxy.get_by_id(User, "user_123")
    await proxy.create(new_user)

For MCP tools that call db methods synchronously from async context:
    sync_proxy = SyncDBServiceProxy(async_proxy)
    # MCP tools can call sync_proxy.get_all(User) synchronously
"""

import asyncio
import concurrent.futures
import logging
import threading
from copy import deepcopy
from typing import Any, TypeVar

from pydantic import BaseModel

from tolokaforge.runner.db_client import DBServiceClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class DBServiceProxy:
    """
    Proxy that looks like InMemoryDatabase but talks to DB Service.

    This proxy allows MCP async tools (TlkMcpCore) to work with the
    centralized DB Service instead of a local InMemoryDatabase.

    The proxy maintains a mapping of Pydantic model classes to table names,
    which is established during initialization based on the task's schema.

    Attributes:
        db_client: HTTP client for DB Service communication
        trial_id: Unique trial identifier for state isolation
        model_to_table: Mapping of model class -> table name
        table_to_model: Mapping of table name -> model class
    """

    def __init__(
        self,
        db_client: DBServiceClient,
        trial_id: str,
        model_registry: dict[str, type[BaseModel]] | None = None,
        db_table_names: list[str] | None = None,
    ):
        """
        Initialize the DB Service proxy.

        Args:
            db_client: HTTP client for DB Service communication
            trial_id: Unique trial identifier (e.g., "airline_task_001:0")
            model_registry: Optional mapping of table_name -> model class
                           If not provided, records are returned as dicts
            db_table_names: Optional list of actual table names from initial_state.
                           Used for fallback table name resolution when model is not registered.
        """
        self.db_client = db_client
        self.trial_id = trial_id
        self.db_table_names = db_table_names or []
        # Domain name for TypeSense search (used by search_policy tools
        # that call getattr(db, "domain") to look up the registry).
        self.domain: str | None = None

        # Model class <-> table name mappings
        # Use fully qualified class name as key to handle module identity issues
        # (same class imported via different paths creates different objects)
        self._model_name_to_table: dict[str, str] = {}
        self._table_to_model: dict[str, type[BaseModel]] = {}

        # Register models if provided
        if model_registry:
            for table_name, model_cls in model_registry.items():
                self.register_model(table_name, model_cls)

        # Local cache for state (optional optimization)
        self._cache: dict[str, list[dict[str, Any]]] | None = None
        self._cache_enabled = False

    def _get_model_key(self, model_cls: type[BaseModel]) -> str:
        """
        Get a stable key for a model class that works across module reloads.

        Uses the fully qualified class name (module + class name) to handle
        cases where the same class is imported via different paths.
        """
        return f"{model_cls.__module__}.{model_cls.__name__}"

    def register_model(self, table_name: str, model_cls: type[BaseModel]) -> None:
        """
        Register a Pydantic model class for a table.

        Args:
            table_name: Name of the table in DB Service
            model_cls: Pydantic model class for records in this table
        """
        model_key = self._get_model_key(model_cls)

        # Check if already registered with a different table name
        if model_key in self._model_name_to_table:
            existing_table = self._model_name_to_table[model_key]
            if existing_table != table_name:
                logger.warning(
                    f"Model {model_cls.__name__} (key={model_key}) already registered for table '{existing_table}', "
                    f"overwriting with '{table_name}'"
                )

        self._model_name_to_table[model_key] = table_name
        self._table_to_model[table_name] = model_cls
        logger.info(
            f"DBServiceProxy.register_model: {model_cls.__name__} (key={model_key}) -> table '{table_name}' (dict_id={id(self._model_name_to_table)})"
        )

    def _get_table_name(self, model_cls: type[BaseModel]) -> str:
        """
        Get the table name for a model class.

        Falls back to matching against db_table_names if not registered,
        then to deriving table name from class name as last resort.
        """
        model_key = self._get_model_key(model_cls)

        # Debug: check exact key matching
        logger.info(
            f"_get_table_name: model_key='{model_key}', "
            f"in_dict={model_key in self._model_name_to_table}, "
            f"dict_id={id(self._model_name_to_table)}, "
            f"dict_len={len(self._model_name_to_table)}"
        )

        if model_key in self._model_name_to_table:
            table_name = self._model_name_to_table[model_key]
            logger.info(f"_get_table_name: FOUND model_key='{model_key}' -> table='{table_name}'")
            return table_name

        # Log the mismatch for debugging
        logger.warning(
            f"Model key '{model_key}' not found in registered models. "
            f"Available keys: {list(self._model_name_to_table.keys())}"
        )

        # Fallback: derive table name suffix from class name
        # Convert CamelCase to snake_case
        name = model_cls.__name__
        snake_name = "".join(["_" + c.lower() if c.isupper() else c for c in name]).lstrip("_")

        # Try to find a matching table in db_table_names
        # This handles the case where tables have namespace prefixes (e.g., tau_manufacturing_capa)
        if self.db_table_names:
            # Build suffixes to match against (both singular and plural)
            snake_suffix_singular = f"_{snake_name}"
            snake_suffix_plural = f"_{snake_name}s"
            # Handle -y -> -ies pluralization
            if snake_name.endswith("y"):
                snake_suffix_plural_ies = f"_{snake_name[:-1]}ies"
            else:
                snake_suffix_plural_ies = None

            for db_table in self.db_table_names:
                # Strategy 1: suffix match singular (e.g., "tau_manufacturing_capa" ends with "_capa")
                if db_table.endswith(snake_suffix_singular):
                    logger.info(
                        f"_get_table_name: Matched model {model_cls.__name__} to table '{db_table}' "
                        f"(singular suffix '{snake_suffix_singular}')"
                    )
                    # Register for future lookups
                    self.register_model(db_table, model_cls)
                    return db_table
                # Strategy 2: suffix match plural (e.g., "zendesk_users" ends with "_users")
                if db_table.endswith(snake_suffix_plural):
                    logger.info(
                        f"_get_table_name: Matched model {model_cls.__name__} to table '{db_table}' "
                        f"(plural suffix '{snake_suffix_plural}')"
                    )
                    # Register for future lookups
                    self.register_model(db_table, model_cls)
                    return db_table
                # Strategy 3: -ies plural suffix (e.g., "entries" for "entry")
                if snake_suffix_plural_ies and db_table.endswith(snake_suffix_plural_ies):
                    logger.info(
                        f"_get_table_name: Matched model {model_cls.__name__} to table '{db_table}' "
                        f"(ies plural suffix '{snake_suffix_plural_ies}')"
                    )
                    # Register for future lookups
                    self.register_model(db_table, model_cls)
                    return db_table
                # Strategy 4: exact match with singular (e.g., "capa" == "capa")
                if db_table == snake_name:
                    logger.info(
                        f"_get_table_name: Matched model {model_cls.__name__} to table '{db_table}' "
                        f"(exact singular match)"
                    )
                    # Register for future lookups
                    self.register_model(db_table, model_cls)
                    return db_table
                # Strategy 5: exact match with plural (e.g., "capas" == "capas")
                if db_table == f"{snake_name}s":
                    logger.info(
                        f"_get_table_name: Matched model {model_cls.__name__} to table '{db_table}' "
                        f"(exact plural match)"
                    )
                    # Register for future lookups
                    self.register_model(db_table, model_cls)
                    return db_table

        # Last resort fallback: derive table name from class name
        # Simple pluralization
        table_name = snake_name
        if not table_name.endswith("s"):
            table_name += "s"

        logger.warning(
            f"Model {model_cls.__name__} not registered and no match in db_table_names, "
            f"using derived table name: {table_name}"
        )
        return table_name

    def _to_model(self, model_cls: type[T], data: dict[str, Any]) -> T:
        """Convert a dict to a Pydantic model instance."""
        try:
            return model_cls.model_validate(data)
        except AttributeError:
            # Pydantic v1 fallback
            return model_cls.parse_obj(data)

    def _to_dict(self, obj: BaseModel) -> dict[str, Any]:
        """Convert a Pydantic model to a dict."""
        try:
            return obj.model_dump(mode="json")
        except AttributeError:
            # Pydantic v1 fallback
            return obj.dict()

    def _get_id(self, obj: BaseModel) -> Any:
        """Get the ID of a Pydantic model instance."""
        # Try get_id() method first (TlkMcpCore convention)
        if hasattr(obj, "get_id"):
            return obj.get_id()

        # Fall back to 'id' attribute
        if hasattr(obj, "id"):
            return obj.id

    def _get_id_field_name(self, obj: BaseModel) -> str:
        """Get the ID field name of a Pydantic model instance."""
        if hasattr(obj, "get_id"):
            import inspect

            try:
                source = inspect.getsource(obj.get_id)
                for line in source.split("\n"):
                    line = line.strip()
                    if line.startswith("return self."):
                        return line.replace("return self.", "").strip()
            except (TypeError, OSError):
                pass
        if hasattr(obj, "id"):
            return "id"
        raise ValueError(f"Cannot determine ID field name for {type(obj).__name__}")

        raise ValueError(f"Cannot determine ID for object of type {type(obj).__name__}")

    # =========================================================================
    # InMemoryDatabase-compatible interface (async versions)
    # =========================================================================

    async def get_all(self, model_cls: type[T]) -> list[T]:
        """
        Get all items of a specific model type from the database.

        Maps to: GET /trials/{trial_id}/state (filtered by table)

        Args:
            model_cls: Pydantic model class

        Returns:
            List of model instances
        """
        table_name = self._get_table_name(model_cls)
        model_key = self._get_model_key(model_cls)

        logger.info(
            f"get_all: model_cls={model_cls.__name__}, model_key={model_key}, "
            f"table_name={table_name}, registered_tables={list(self._model_name_to_table.keys())}"
        )

        response = await self.db_client.get_state(self.trial_id, tables=[table_name])

        # StateResponse is a Pydantic model with .data attribute
        records = response.data.get(table_name, [])

        logger.debug(
            f"get_all: table_name={table_name}, records_count={len(records)}, "
            f"available_tables={list(response.data.keys())}"
        )

        return [self._to_model(model_cls, record) for record in records]

    async def get_by_id(self, model_cls: type[T], value: Any) -> T | None:
        """
        Get a single item by its ID from the database.

        Uses JSONPath query to find the record. Tries multiple ID field names
        since different models use different fields as their primary key:
        - Most models use 'id'
        - Some models (like Employee) use 'email' as the primary key

        Args:
            model_cls: Pydantic model class
            value: ID value to search for

        Returns:
            Model instance or None if not found
        """
        table_name = self._get_table_name(model_cls)

        # Try common ID field names in order of likelihood
        # Most models use 'id', but some use 'email' (e.g., Employee model)
        id_fields = ["id", "email"]

        for id_field in id_fields:
            # Use JSONPath query to find by ID field
            jsonpath = f"$.{table_name}[?(@.{id_field}=='{value}')]"

            try:
                response = await self.db_client.query(self.trial_id, jsonpath)
                # QueryResponse is a Pydantic model with .results attribute
                results = response.results

                if results:
                    return self._to_model(model_cls, results[0])
            except Exception as e:
                logger.debug(f"JSONPath query for {id_field} failed: {e}")
                continue

        # Fallback: get all and filter using model's get_id() method
        # This handles any custom ID field that we didn't try above
        logger.debug(f"JSONPath queries failed, falling back to full scan for {model_cls.__name__}")
        all_items = await self.get_all(model_cls)
        for item in all_items:
            if self._get_id(item) == value:
                return item
        return None

    async def create(self, obj: BaseModel) -> BaseModel:
        """
        Create a new object in the database.

        Maps to: PATCH /trials/{trial_id}/state/{table} with insert operation

        Args:
            obj: Pydantic model instance to create

        Returns:
            The created model instance

        Raises:
            ValueError: If object with same ID already exists
        """
        model_cls = obj.__class__
        table_name = self._get_table_name(model_cls)
        record = self._to_dict(obj)

        # Check if ID already exists
        obj_id = self._get_id(obj)
        existing = await self.get_by_id(model_cls, obj_id)
        if existing is not None:
            raise ValueError(f"Object with ID {obj_id} already exists")

        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "insert", "record": record}],
        )

        return obj

    async def bulk_create(self, objects: list[BaseModel]) -> list[BaseModel]:
        """
        Create multiple objects in the database.

        Args:
            objects: List of Pydantic model instances to create

        Returns:
            List of created model instances
        """
        if not objects:
            return []

        model_cls = objects[0].__class__
        table_name = self._get_table_name(model_cls)

        operations = []
        for obj in objects:
            if obj.__class__ != model_cls:
                raise ValueError("All objects must be of the same model class")
            record = self._to_dict(obj)
            operations.append({"op": "insert", "record": record})

        await self.db_client.mutate(
            trial_id=self.trial_id, table_name=table_name, operations=operations
        )

        return objects

    async def update(self, obj: BaseModel) -> BaseModel:
        """
        Update an existing object in the database.

        Maps to: PATCH /trials/{trial_id}/state/{table} with update operation

        Args:
            obj: Pydantic model instance with updated values

        Returns:
            The updated model instance

        Raises:
            ValueError: If object with ID doesn't exist
        """
        model_cls = obj.__class__
        table_name = self._get_table_name(model_cls)
        obj_id = self._get_id(obj)
        record = self._to_dict(obj)

        # Check if ID exists
        existing = await self.get_by_id(model_cls, obj_id)
        if existing is None:
            raise ValueError(f"Object with ID {obj_id} does not exist")

        # Use upsert to replace the entire record
        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "upsert", "record": record, "key": self._get_id_field_name(obj)}],
        )

        return obj

    async def delete(self, obj: BaseModel) -> None:
        """
        Delete an existing object from the database.

        Maps to: PATCH /trials/{trial_id}/state/{table} with delete operation

        Args:
            obj: Pydantic model instance to delete

        Raises:
            ValueError: If object with ID doesn't exist
        """
        model_cls = obj.__class__
        table_name = self._get_table_name(model_cls)
        obj_id = self._get_id(obj)

        # Check if ID exists
        existing = await self.get_by_id(model_cls, obj_id)
        if existing is None:
            raise ValueError(f"Object with ID {obj_id} does not exist")

        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "delete", "filter": {"id": obj_id}}],
        )

    async def delete_by_id(self, model_cls: type[T], obj_id: Any) -> None:
        """
        Delete an existing object from the database by its ID.

        Args:
            model_cls: Pydantic model class
            obj_id: ID of the object to delete

        Raises:
            ValueError: If object with ID doesn't exist
        """
        table_name = self._get_table_name(model_cls)

        # Check if ID exists
        existing = await self.get_by_id(model_cls, obj_id)
        if existing is None:
            raise ValueError(f"Object with ID {obj_id} does not exist")

        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "delete", "filter": {"id": obj_id}}],
        )

    async def bulk_delete(self, objects: list[BaseModel]) -> None:
        """
        Delete multiple objects from the database.

        Args:
            objects: List of Pydantic model instances to delete
        """
        if not objects:
            return

        model_cls = objects[0].__class__
        table_name = self._get_table_name(model_cls)

        operations = []
        for obj in objects:
            if obj.__class__ != model_cls:
                raise ValueError("All objects must be of the same model class")
            obj_id = self._get_id(obj)
            operations.append({"op": "delete", "filter": {"id": obj_id}})

        await self.db_client.mutate(
            trial_id=self.trial_id, table_name=table_name, operations=operations
        )

    # =========================================================================
    # Additional utility methods
    # =========================================================================

    async def to_state_dict(self) -> dict[str, Any]:
        """
        Get database state as a dictionary.

        Returns:
            Dictionary representation of database state
        """
        response = await self.db_client.get_state(self.trial_id)
        # StateResponse is a Pydantic model with .data attribute
        return response.data

    async def get_stable_hash(self) -> str:
        """
        Get the stable hash of the current state.

        Returns:
            SHA-256 hash of stable state
        """
        return await self.db_client.get_stable_hash(self.trial_id)

    def copy(self) -> "DBServiceProxy":
        """
        Create a copy of this proxy (shares the same DB client and trial).

        Note: This doesn't copy the actual database state, just the proxy config.
        For state isolation, use snapshots via DB Service.
        """
        new_proxy = DBServiceProxy(
            db_client=self.db_client,
            trial_id=self.trial_id,
            db_table_names=list(self.db_table_names),
        )
        new_proxy._model_name_to_table = deepcopy(self._model_name_to_table)
        new_proxy._table_to_model = deepcopy(self._table_to_model)
        return new_proxy


class SyncDBServiceProxy:
    """
    Synchronous wrapper around DBServiceProxy for tools that need sync DB access.

    This wrapper is used by:
    1. Tau tools - which use synchronous invoke() methods
    2. MCP tools - which call db methods synchronously inside async run() methods

    The wrapper handles both cases:
    - When called from a sync context: creates a new event loop
    - When called from an async context: runs in a thread pool to avoid blocking

    This is necessary because MCP tools have async run() methods but call
    db.get_all(), db.create(), etc. synchronously inside them.
    """

    # Thread pool for running async operations from within async context
    _executor: concurrent.futures.ThreadPoolExecutor | None = None
    _executor_lock = threading.Lock()

    def __init__(self, async_proxy: DBServiceProxy):
        """
        Initialize the sync wrapper.

        Args:
            async_proxy: The async DBServiceProxy to wrap
        """
        self._async_proxy = async_proxy

    @property
    def domain(self) -> str | None:
        """Forward domain attribute from the underlying async proxy."""
        return self._async_proxy.domain

    @domain.setter
    def domain(self, value: str | None) -> None:
        self._async_proxy.domain = value

    @property
    def _stem_to_model_cls(self) -> dict[str, type]:
        """Compatibility with InMemoryDatabase - maps model names to model classes."""
        return {cls.__name__: cls for cls in self._async_proxy._table_to_model.values()}

    @classmethod
    def _get_executor(cls) -> concurrent.futures.ThreadPoolExecutor:
        """Get or create the shared thread pool executor."""
        if cls._executor is None:
            with cls._executor_lock:
                if cls._executor is None:
                    cls._executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=10, thread_name_prefix="sync_db_proxy"
                    )
        return cls._executor

    def _run_async(self, coro):
        """
        Run an async coroutine synchronously.

        Handles both cases:
        - From sync context: creates/uses event loop directly
        - From async context: runs in thread pool to avoid blocking
        """
        # Check if we're in an async context (running event loop)
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context - run in thread pool
            return self._run_in_thread(coro)
        except RuntimeError:
            # No running loop - we're in a sync context
            pass

        # Try to get or create an event loop for sync context
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Loop exists but is running - use thread pool
                return self._run_in_thread(coro)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(coro)

    def _run_in_thread(self, coro):
        """
        Run an async coroutine in a thread pool.

        This is used when we're called from within an async context
        and can't use run_until_complete() on the current loop.
        """

        def run_coro():
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        executor = self._get_executor()
        future = executor.submit(run_coro)
        return future.result()

    @property
    def trial_id(self) -> str:
        return self._async_proxy.trial_id

    @property
    def db_client(self):
        """Expose db_client for state sync operations."""
        return self._async_proxy.db_client

    def register_model(self, table_name: str, model_cls: type[BaseModel]) -> None:
        self._async_proxy.register_model(table_name, model_cls)

    def get_all(self, model_cls: type[T]) -> list[T]:
        return self._run_async(self._async_proxy.get_all(model_cls))

    def get_by_id(self, model_cls: type[T], value: Any) -> T | None:
        return self._run_async(self._async_proxy.get_by_id(model_cls, value))

    def create(self, obj: BaseModel) -> BaseModel:
        return self._run_async(self._async_proxy.create(obj))

    def bulk_create(self, objects: list[BaseModel]) -> list[BaseModel]:
        return self._run_async(self._async_proxy.bulk_create(objects))

    def update(self, obj: BaseModel) -> BaseModel:
        return self._run_async(self._async_proxy.update(obj))

    def delete(self, obj: BaseModel) -> None:
        return self._run_async(self._async_proxy.delete(obj))

    def delete_by_id(self, model_cls: type[T], obj_id: Any) -> None:
        return self._run_async(self._async_proxy.delete_by_id(model_cls, obj_id))

    def bulk_delete(self, objects: list[BaseModel]) -> None:
        return self._run_async(self._async_proxy.bulk_delete(objects))

    def to_state_dict(self) -> dict[str, Any]:
        return self._run_async(self._async_proxy.to_state_dict())

    def copy(self) -> "SyncDBServiceProxy":
        return SyncDBServiceProxy(self._async_proxy.copy())
