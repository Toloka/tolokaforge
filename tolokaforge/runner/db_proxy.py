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
import math
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from tolokaforge.runner.db_client import DBServiceClient, DBServiceError
from tolokaforge.runner.id_resolution import (
    IdFieldResolutionError,
    TableKey,
    resolve_record_id,
    table_key,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


__all__ = ["DBServiceProxy", "SyncDBServiceProxy", "IdFieldResolutionError"]


def _jsonpath_literal(value: Any) -> str | None:
    """The JSONPath filter literal for ``value``, or ``None`` if it has none.

    Encoded by JSON type, ``bool`` ahead of ``int`` because ``bool`` subclasses
    it and the two spell different literals. A float renders only when its
    ``repr`` carries no exponent — the dialect's number token rejects
    ``1e+20``. ``None``, non-finite floats and containers have no literal at
    all: ``==null`` and ``==None`` match nothing rather than matching a stored
    ``null``.

    A literal is a filter, never an authority: the dialect compares numerically
    by coercing the stored value (``@.id==0`` matches a stored ``0.1``), so a
    hit is a candidate the caller must still verify.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rendered = repr(float(value))
        return None if "e" in rendered or "E" in rendered else rendered
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return None


def _declares_default_key_field(model_cls: type[BaseModel]) -> bool:
    """Whether ``model_cls`` carries the literal ``id`` the default key names."""
    return "id" in getattr(model_cls, "model_fields", {})


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
        id_fields: Mapping[str, str | list[str]] | None = None,
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
            id_fields: Optional per-table primary-key overrides (table_name -> key
                           field or ordered component list), from grading config
                           state_checks.id_fields. A table absent from the map
                           resolves to the literal "id".
        """
        self.db_client = db_client
        self.trial_id = trial_id
        self.db_table_names = db_table_names or []
        # Per-table primary-key override (table_name -> key field or ordered
        # component list). Data-driven so upsert/delete/lookup key resolution
        # never depends on reading model source at runtime; a table absent here
        # resolves to "id".
        self._id_fields: dict[str, str | list[str]] = dict(id_fields or {})
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
        logger.debug(
            f"DBServiceProxy.register_model: {model_cls.__name__} (key={model_key}) -> table '{table_name}' (dict_id={id(self._model_name_to_table)})"
        )

    def _get_table_name(self, model_cls: type[BaseModel]) -> str:
        """
        Get the table name for a model class.

        Falls back to matching against db_table_names if not registered,
        then to deriving table name from class name as last resort.
        """
        model_key = self._get_model_key(model_cls)

        logger.debug(
            f"_get_table_name: model_key='{model_key}', "
            f"in_dict={model_key in self._model_name_to_table}, "
            f"dict_id={id(self._model_name_to_table)}, "
            f"dict_len={len(self._model_name_to_table)}"
        )

        if model_key in self._model_name_to_table:
            table_name = self._model_name_to_table[model_key]
            logger.debug(f"_get_table_name: FOUND model_key='{model_key}' -> table='{table_name}'")
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

    def _resolve_table_key(self, model_cls: type[BaseModel]) -> TableKey:
        """Ordered primary-key fields for a table's write/delete operations.

        Config-first (``state_checks.id_fields``), literal-``"id"`` default, fail
        loud. When no config is provided, the default is validated against the
        model's declared fields so a natural-key table (``widget_id`` etc.) with
        no override raises with the exact YAML to add rather than silently using
        ``"id"``.
        """
        table_name = self._get_table_name(model_cls)
        if self._id_fields.get(table_name):
            return table_key(table_name, self._id_fields)
        if _declares_default_key_field(model_cls):
            return TableKey(("id",))
        raise IdFieldResolutionError(
            f"Cannot determine ID field for table '{table_name}' "
            f"(model {model_cls.__name__}): no 'id' field and no "
            f"state_checks.id_fields entry. Add to the task's grading.yaml:\n"
            f"  state_checks:\n    id_fields:\n"
            f"      {table_name}: <key_field>  # or [<field_1>, <field_2>] for a composite key"
        )

    def _key_components_from_record(
        self, table_name: str, key: TableKey, record: dict[str, Any]
    ) -> dict[str, Any]:
        """A composite-keyed record's key value as a component mapping.

        Never derived from ``get_id()`` — how a pack's model concatenates its
        components is invisible to the engine, so the record dict is the only
        authority on the key value.
        """
        components = resolve_record_id(record, table_name, self._id_fields)
        return dict(zip(key.fields, components, strict=True))

    def _composite_lookup_components(
        self, table_name: str, key: TableKey, value: Any
    ) -> dict[str, Any]:
        """Validate a caller-supplied lookup value against a composite key.

        A mapping of component values is the only accepted shape: a scalar
        cannot address a composite key, and a bare sequence is ambiguous
        between "two components" and "one component whose value is a list".
        """
        if not isinstance(value, Mapping):
            example = ", ".join(f'"{f}": ...' for f in key.fields)
            raise IdFieldResolutionError(
                f"Table {table_name!r} declares composite key {list(key.fields)!r}; "
                f"address records with a mapping of component values "
                f"({{{example}}}), got {value!r}."
            )
        missing = sorted(set(key.fields) - set(value))
        unexpected = sorted(set(value) - set(key.fields))
        if missing or unexpected:
            raise IdFieldResolutionError(
                f"Composite lookup for table {table_name!r} must supply exactly "
                f"the declared key components {list(key.fields)!r}: "
                f"missing={missing}, unexpected={unexpected}."
            )
        return {field: value[field] for field in key.fields}

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

        logger.debug(
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

        Resolves the table's key from config (state_checks.id_fields, default
        "id"). A single-field table takes a scalar; a composite-keyed table
        takes a mapping of component values (a scalar or bare sequence raises
        IdFieldResolutionError naming the components).

        Each shape's answer is defined by one predicate, evaluated by a full
        scan: ``get_id(item) == value`` for a single-field key, and every
        declared component equal to the record's own value for a composite one.
        The JSONPath lookup is only an index over that predicate. A key whose
        every component has a literal resolves in one query, but a hit counts
        only once the validated model satisfies the same predicate, so a row
        the dialect's numeric coercion dragged in — or one the model cannot
        represent — is a miss rather than a second, looser notion of equality.
        Every kind of miss (no literal, a query error, an unverified hit, no
        usable key to query at all) falls to the scan, which is why the query
        can only ever save a round trip, never change an answer.

        Args:
            model_cls: Pydantic model class
            value: Key value — scalar for a single-field key, component
                mapping for a composite key

        Returns:
            Model instance or None if not found
        """
        table_name = self._get_table_name(model_cls)
        key = table_key(table_name, self._id_fields)
        if len(key.fields) > 1:
            return await self._get_by_composite_key(model_cls, table_name, key, value)

        return await self._get_by_single_key(model_cls, table_name, key.fields[0], value)

    async def _get_by_single_key(
        self, model_cls: type[T], table_name: str, id_field: str, value: Any
    ) -> T | None:
        """Single-field lookup: one typed JSONPath predicate, then a full scan
        comparing ``get_id()`` — a runtime value call, never model source, so a
        key the config never declared (an email, say) still resolves."""
        if not self._id_fields.get(table_name) and not _declares_default_key_field(model_cls):
            logger.debug(
                f"get_by_id: table '{table_name}' declares no key field and "
                f"{model_cls.__name__} has no 'id' field — no query issued, full scan"
            )
        else:
            hit = await self._query_key(
                model_cls,
                table_name,
                f"{table_name}.{id_field}",
                {id_field: value},
                lambda model: self._get_id(model) == value,
            )
            if hit is not None:
                return hit

        for item in await self.get_all(model_cls):
            if self._get_id(item) == value:
                return item
        return None

    async def _get_by_composite_key(
        self, model_cls: type[T], table_name: str, key: TableKey, value: Any
    ) -> T | None:
        """Composite lookup: one typed JSONPath predicate per component, then a
        component-wise full scan — never ``get_id()``, whose concatenation the
        engine cannot interpret."""
        components = self._composite_lookup_components(table_name, key, value)

        hit = await self._query_key(
            model_cls,
            table_name,
            f"{table_name}.{'+'.join(key.fields)}",
            components,
            lambda model: self._satisfies_components(self._to_dict(model), components),
        )
        if hit is not None:
            return hit

        for item in await self.get_all(model_cls):
            if self._satisfies_components(self._to_dict(item), components):
                return item
        return None

    @staticmethod
    def _satisfies_components(record: dict[str, Any], components: dict[str, Any]) -> bool:
        """Whether ``record`` carries every declared key component's value."""
        return all(record.get(field) == component for field, component in components.items())

    async def _query_key(
        self,
        model_cls: type[T],
        table_name: str,
        target: str,
        components: dict[str, Any],
        satisfies: Callable[[T], bool],
    ) -> T | None:
        """One indexed lookup for ``components``, or ``None`` to fall to the scan.

        Single-field and composite keys share this: the difference between them
        is not how a key is queried but what "found" means, so each path passes
        ``satisfies`` — *its own scan's predicate* — and a hit is a hit only if
        it holds. That equality is the whole safety argument: the query is an
        index over the scan, so it can save a round trip but never change an
        answer, and every way of declining below falls through to the scan.

        Declines at the level that matches who can act: a component with no
        literal and a failed query are operator-facing (info, warning), while a
        miss stays at debug because ``create()`` asks this question about every
        record it is about to insert and legitimately expects "not there".
        """
        literals = {field: _jsonpath_literal(value) for field, value in components.items()}
        unencodable = [field for field, literal in literals.items() if literal is None]
        if unencodable:
            detail = ", ".join(
                f"{field}={components[field]!r} ({type(components[field]).__name__})"
                for field in unencodable
            )
            logger.info(
                f"get_by_id: key component(s) of '{target}' have no JSONPath literal in this "
                f"dialect ({detail}) — no query issued, full scan"
            )
            return None

        predicate = " & ".join(f"@.{field}=={literals[field]}" for field in components)
        try:
            response = await self.db_client.query(self.trial_id, f"$.{table_name}[?({predicate})]")
        except DBServiceError as e:
            logger.warning(f"get_by_id: JSONPath lookup on '{target}' failed: {e} — full scan")
            return None

        for record in response.results:
            verified = self._verified_hit(model_cls, record, satisfies)
            if verified is not None:
                return verified

        logger.debug(
            f"get_by_id: declared key lookup on '{target}' missed for {components!r} — full scan"
        )
        return None

    def _verified_hit(
        self, model_cls: type[T], record: dict[str, Any], satisfies: Callable[[T], bool]
    ) -> T | None:
        """``record`` as a model if it satisfies the caller's predicate, else ``None``.

        A record the model cannot validate is a miss too, not an error: it is
        the same coercion artifact this step exists to reject — a lookup of
        ``7`` against a str-keyed model matches both a stored ``"7"``, which
        the model contradicts, and a stored ``7``, which it cannot hold at all.
        A record that genuinely belongs to the table and defeats its model
        still surfaces, raised by the scan that reads the same rows.

        Validating here is not an extra step either path could skip: both
        scans read models, so a stored ``"2"`` in an ``int``-typed component is
        a ``2`` to the predicate, and comparing the raw record instead would
        make the query miss rows its own scan then finds.
        """
        try:
            model = self._to_model(model_cls, record)
        except ValidationError:
            return None
        return model if satisfies(model) else None

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
        key = self._resolve_table_key(model_cls)
        record = self._to_dict(obj)

        wire_key: str | list[str]
        lookup: Any
        if len(key.fields) == 1:
            lookup = self._get_id(obj)
            wire_key = key.fields[0]
        else:
            lookup = self._key_components_from_record(table_name, key, record)
            wire_key = list(key.fields)

        existing = await self.get_by_id(model_cls, lookup)
        if existing is None:
            raise ValueError(f"Object with ID {lookup} does not exist")

        # Use upsert to replace the entire record
        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "upsert", "record": record, "key": wire_key}],
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
        key = self._resolve_table_key(model_cls)

        lookup: Any
        if len(key.fields) == 1:
            lookup = self._get_id(obj)
            delete_filter = {key.fields[0]: lookup}
        else:
            delete_filter = self._key_components_from_record(table_name, key, self._to_dict(obj))
            lookup = delete_filter

        existing = await self.get_by_id(model_cls, lookup)
        if existing is None:
            raise ValueError(f"Object with ID {lookup} does not exist")

        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "delete", "filter": delete_filter}],
        )

    async def delete_by_id(self, model_cls: type[T], obj_id: Any) -> None:
        """
        Delete an existing object from the database by its ID.

        Args:
            model_cls: Pydantic model class
            obj_id: Key value of the object to delete — scalar for a
                single-field key, component mapping for a composite key

        Raises:
            ValueError: If object with ID doesn't exist
        """
        table_name = self._get_table_name(model_cls)
        key = self._resolve_table_key(model_cls)

        if len(key.fields) == 1:
            delete_filter = {key.fields[0]: obj_id}
            lookup: Any = obj_id
        else:
            delete_filter = self._composite_lookup_components(table_name, key, obj_id)
            lookup = delete_filter

        existing = await self.get_by_id(model_cls, lookup)
        if existing is None:
            raise ValueError(f"Object with ID {obj_id} does not exist")

        await self.db_client.mutate(
            trial_id=self.trial_id,
            table_name=table_name,
            operations=[{"op": "delete", "filter": delete_filter}],
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
        key = self._resolve_table_key(model_cls)

        operations = []
        for obj in objects:
            if obj.__class__ != model_cls:
                raise ValueError("All objects must be of the same model class")
            if len(key.fields) == 1:
                delete_filter = {key.fields[0]: self._get_id(obj)}
            else:
                delete_filter = self._key_components_from_record(
                    table_name, key, self._to_dict(obj)
                )
            operations.append({"op": "delete", "filter": delete_filter})

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
            id_fields=dict(self._id_fields),
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
