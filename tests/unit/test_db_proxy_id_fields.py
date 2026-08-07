"""Config-driven per-table id-field resolution in DBServiceProxy.

Covers the fix for the fragile ``inspect.getsource``-based key resolution that
broke upserts/deletes for tables keyed by a natural key (not the literal ``id``,
e.g. a ``widget_id`` column) whenever the model's source file was not readable at
runtime.

The proxy resolves the key from config (``state_checks.id_fields``) with a
literal ``"id"`` default, so behaviour is data-driven and stable across
runtimes. A declared key is one field name or an ordered component list; the
proxy addresses a composite-keyed record component-wise from the record dict,
never through ``get_id()`` (whose concatenation the engine cannot interpret).

Two harnesses, deliberately:

- ``FakeDBClient`` records the mutation payloads the proxy emits and answers
  query/get_state from an in-memory store it never mutates — it locks payload
  *shape* only. Its ``query()`` regex matches a single-predicate JSONPath, so
  composite lookups exercise the full-scan fallback here.
- The ``db_client`` fixture wraps the real in-process db-service, the only
  harness on which "which row changed" is answerable — every row-level
  assertion lives there.
"""

import inspect
import re
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tolokaforge.runner.db_proxy import DBServiceProxy, IdFieldResolutionError
from tolokaforge.runner.id_resolution import TableKey

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class WidgetLike(BaseModel):
    """Keyed by ``widget_id`` (no ``id`` field) — the shape that broke the old path."""

    model_config = {"extra": "forbid"}

    widget_id: str
    status: str = "new"

    def get_id(self) -> str:
        return self.widget_id


class IdKeyed(BaseModel):
    """Keyed by the literal ``id`` — the default, must stay byte-for-byte unchanged."""

    model_config = {"extra": "forbid"}

    id: str
    name: str = ""

    def get_id(self) -> str:
        return self.id


class Position(BaseModel):
    """Composite-keyed by ``(account_id, symbol)`` — no single column is unique.

    ``get_id`` concatenates, as pack models do; the engine must never consult
    it on a composite path, because the concatenation is uninterpretable
    (``"a_b" + "c"`` vs ``"a" + "b_c"``).
    """

    model_config = {"extra": "forbid"}

    account_id: str
    symbol: str
    qty: int

    def get_id(self) -> str:
        return f"{self.account_id}_{self.symbol}"


class EmailKeyed(BaseModel):
    """Keyed by ``email`` — read must still work unconfigured via the fallback scan."""

    model_config = {"extra": "forbid"}

    email: str
    name: str = ""

    def get_id(self) -> str:
        return self.email


def _make_dynamic_model() -> type[BaseModel]:
    """Define a model via ``exec()`` so ``inspect.getsource(get_id)`` raises OSError,
    reproducing the runtime condition (source not on disk) that broke the old resolver."""
    ns: dict = {}
    exec(
        "from pydantic import BaseModel\n"
        "class Dyn(BaseModel):\n"
        "    model_config = {'extra': 'forbid'}\n"
        "    dyn_id: str\n"
        "    def get_id(self):\n"
        "        return self.dyn_id\n",
        ns,
    )
    return ns["Dyn"]


# ---------------------------------------------------------------------------
# Fake DB client
# ---------------------------------------------------------------------------


class FakeDBClient:
    """Records mutation payloads; answers query/get_state from an in-memory store."""

    def __init__(self, state: dict | None = None):
        self.state = {k: [dict(r) for r in v] for k, v in (state or {}).items()}
        self.mutations: list[tuple[str, list[dict]]] = []
        self.queries: list[str] = []

    async def query(self, trial_id, jsonpath):
        self.queries.append(jsonpath)
        m = re.match(
            r"\$\.(?P<table>[^\[]+)\[\?\(@\.(?P<field>[^=]+)=='(?P<value>[^']*)'\)\]",
            jsonpath,
        )
        results = []
        if m:
            for rec in self.state.get(m["table"], []):
                if str(rec.get(m["field"])) == m["value"]:
                    results.append(rec)
        return SimpleNamespace(results=results)

    async def get_state(self, trial_id, tables=None):
        keys = tables if tables is not None else list(self.state)
        return SimpleNamespace(data={t: self.state.get(t, []) for t in keys})

    async def mutate(self, trial_id, table_name, operations):
        self.mutations.append((table_name, operations))
        return SimpleNamespace(success=True)


def _proxy(table, model_cls, *, id_fields=None, state=None):
    client = FakeDBClient(state=state)
    proxy = DBServiceProxy(
        client,
        "trial:0",
        model_registry={table: model_cls},
        db_table_names=[table],
        id_fields=id_fields,
    )
    return proxy, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_update_uses_configured_key():
    proxy, client = _proxy(
        "widgets",
        WidgetLike,
        id_fields={"widgets": "widget_id"},
        state={"widgets": [{"widget_id": "W1", "status": "new"}]},
    )
    await proxy.update(WidgetLike(widget_id="W1", status="ready"))
    table, ops = client.mutations[-1]
    assert table == "widgets"
    assert ops[0]["op"] == "upsert"
    assert ops[0]["key"] == "widget_id"  # config-resolved, not "id"


async def test_update_defaults_to_id_unchanged():
    proxy, client = _proxy(
        "items",
        IdKeyed,
        state={"items": [{"id": "X1", "name": "a"}]},
    )
    await proxy.update(IdKeyed(id="X1", name="b"))
    _, ops = client.mutations[-1]
    assert ops[0]["key"] == "id"  # default path byte-for-byte unchanged


async def test_delete_uses_configured_key():
    proxy, client = _proxy(
        "widgets",
        WidgetLike,
        id_fields={"widgets": "widget_id"},
        state={"widgets": [{"widget_id": "W1", "status": "new"}]},
    )
    await proxy.delete(WidgetLike(widget_id="W1"))
    _, ops = client.mutations[-1]
    assert ops[0]["op"] == "delete"
    assert ops[0]["filter"] == {"widget_id": "W1"}  # not {"id": ...} (was a silent no-op)


async def test_get_by_id_queries_only_configured_field():
    proxy, client = _proxy(
        "widgets",
        WidgetLike,
        id_fields={"widgets": "widget_id"},
        state={"widgets": [{"widget_id": "W1", "status": "new"}]},
    )
    found = await proxy.get_by_id(WidgetLike, "W1")
    assert found is not None and found.widget_id == "W1"
    assert any("@.widget_id==" in q for q in client.queries)
    assert not any("@.id==" in q for q in client.queries)  # never probes literal "id"


async def test_resolve_table_key_fails_loud_when_unconfigured():
    proxy, _ = _proxy("widgets", WidgetLike)  # no id_fields, model has no "id"
    with pytest.raises(IdFieldResolutionError) as ei:
        proxy._resolve_table_key(WidgetLike)
    msg = str(ei.value)
    assert "widgets" in msg and "id_fields" in msg  # actionable: names table + the fix
    assert "<key_field>" in msg and "[<field_1>, <field_2>]" in msg  # both key forms


async def test_resolve_table_key_immune_to_missing_source():
    """The old resolver parsed ``inspect.getsource(get_id)``; a model whose source is
    unavailable made it raise. The config path must resolve regardless."""
    Dyn = _make_dynamic_model()
    with pytest.raises(OSError):
        inspect.getsource(Dyn.get_id)  # confirm the source is genuinely unreadable
    proxy, _ = _proxy("dyn", Dyn, id_fields={"dyn": "dyn_id"})
    assert proxy._resolve_table_key(Dyn) == TableKey(("dyn_id",))


async def test_email_keyed_read_works_unconfigured_via_fallback():
    proxy, _ = _proxy(
        "users",
        EmailKeyed,
        state={"users": [{"email": "a@x.io", "name": "A"}]},
    )
    found = await proxy.get_by_id(EmailKeyed, "a@x.io")
    assert found is not None and found.email == "a@x.io"  # full-scan fallback


def test_copy_preserves_id_fields():
    proxy, _ = _proxy("widgets", WidgetLike, id_fields={"widgets": "widget_id"})
    assert proxy.copy()._id_fields == {"widgets": "widget_id"}


def test_state_checks_config_rejects_blank_id_field():
    """The config validator fails loud on blank table names / key fields, so a
    malformed override can never reach the proxy (where blank folds to 'id')."""
    from pydantic import ValidationError

    from tolokaforge.runner.models import RunnerStateChecksConfig

    with pytest.raises(ValidationError):
        RunnerStateChecksConfig(id_fields={"widgets": ""})  # blank key field
    with pytest.raises(ValidationError):
        RunnerStateChecksConfig(id_fields={"  ": "widget_id"})  # blank table name
    assert RunnerStateChecksConfig(id_fields={"widgets": "widget_id"}).id_fields == {
        "widgets": "widget_id"
    }


# ---------------------------------------------------------------------------
# Composite keys — payload shape (FakeDBClient records ops, never applies them)
# ---------------------------------------------------------------------------


POSITIONS = [
    {"account_id": "A1", "symbol": "AAPL", "qty": 10},
    {"account_id": "A1", "symbol": "MSFT", "qty": 5},
    {"account_id": "A2", "symbol": "MSFT", "qty": 7},
]

COMPOSITE = {"positions": ["account_id", "symbol"]}


def _positions_proxy(**kwargs):
    return _proxy("positions", Position, id_fields=dict(COMPOSITE), **kwargs)


async def test_composite_update_emits_list_key_upsert():
    proxy, client = _positions_proxy(state={"positions": POSITIONS})
    await proxy.update(Position(account_id="A1", symbol="MSFT", qty=99))
    table, ops = client.mutations[-1]
    assert table == "positions"
    assert ops[0]["op"] == "upsert"
    assert ops[0]["key"] == ["account_id", "symbol"]  # ordered component list, never get_id()
    assert ops[0]["record"] == {"account_id": "A1", "symbol": "MSFT", "qty": 99}


async def test_composite_delete_filter_carries_every_component():
    proxy, client = _positions_proxy(state={"positions": POSITIONS})
    await proxy.delete(Position(account_id="A1", symbol="MSFT", qty=5))
    _, ops = client.mutations[-1]
    assert ops[0]["op"] == "delete"
    assert ops[0]["filter"] == {"account_id": "A1", "symbol": "MSFT"}  # components only


async def test_composite_bulk_delete_builds_component_filters():
    proxy, client = _positions_proxy(state={"positions": POSITIONS})
    await proxy.bulk_delete(
        [
            Position(account_id="A1", symbol="AAPL", qty=10),
            Position(account_id="A2", symbol="MSFT", qty=7),
        ]
    )
    _, ops = client.mutations[-1]
    assert [op["filter"] for op in ops] == [
        {"account_id": "A1", "symbol": "AAPL"},
        {"account_id": "A2", "symbol": "MSFT"},
    ]


async def test_composite_scalar_lookup_is_refused_naming_components():
    """``"A1_MSFT"`` is a pack model's private concatenation — the proxy cannot
    interpret it against a composite key, so it refuses rather than guessing."""
    proxy, client = _positions_proxy(state={"positions": POSITIONS})
    with pytest.raises(IdFieldResolutionError) as ei:
        await proxy.get_by_id(Position, "A1_MSFT")
    msg = str(ei.value)
    assert "positions" in msg
    assert "account_id" in msg and "symbol" in msg
    with pytest.raises(IdFieldResolutionError):
        await proxy.delete_by_id(Position, "A1_MSFT")
    assert client.mutations == []  # nothing was written


async def test_composite_sequence_lookup_is_refused():
    """A bare sequence is ambiguous between "two components" and "one component
    whose value is a list" — only a mapping is accepted."""
    proxy, _ = _positions_proxy(state={"positions": POSITIONS})
    with pytest.raises(IdFieldResolutionError):
        await proxy.get_by_id(Position, ["A1", "MSFT"])


async def test_composite_lookup_mapping_must_cover_the_declared_key():
    proxy, _ = _positions_proxy(state={"positions": POSITIONS})
    with pytest.raises(IdFieldResolutionError) as ei:
        await proxy.get_by_id(Position, {"account_id": "A1"})
    assert "missing=['symbol']" in str(ei.value)
    with pytest.raises(IdFieldResolutionError) as ei:
        await proxy.get_by_id(Position, {"account_id": "A1", "symbol": "MSFT", "qty": 5})
    assert "unexpected=['qty']" in str(ei.value)


# ---------------------------------------------------------------------------
# Composite keys — row-level outcomes (real in-process db-service applies ops)
# ---------------------------------------------------------------------------


async def _service_proxy(db_client, trial_id, table, model_cls, rows, id_fields):
    await db_client.init_trial(trial_id, {table: [dict(r) for r in rows]})
    return DBServiceProxy(
        db_client,
        trial_id,
        model_registry={table: model_cls},
        db_table_names=[table],
        id_fields=id_fields,
    )


async def _rows(db_client, trial_id, table):
    return (await db_client.get_state(trial_id)).data[table]


async def test_composite_update_changes_only_the_fully_matching_row(db_client):
    """The reproduction as an assertion: on main the nearest legal declaration
    (``positions: account_id``) makes this same update destroy the A1/AAPL row."""
    proxy = await _service_proxy(
        db_client, "t_proxy_comp_update", "positions", Position, POSITIONS, dict(COMPOSITE)
    )
    await proxy.update(Position(account_id="A1", symbol="MSFT", qty=99))
    assert await _rows(db_client, "t_proxy_comp_update", "positions") == [
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "A1", "symbol": "MSFT", "qty": 99},
        {"account_id": "A2", "symbol": "MSFT", "qty": 7},
    ]


async def test_composite_delete_removes_exactly_that_row(db_client):
    """On main the same call is a silent no-op — get_id()'s ``"A1_MSFT"`` against
    the ``account_id`` column matches nothing."""
    proxy = await _service_proxy(
        db_client, "t_proxy_comp_delete", "positions", Position, POSITIONS, dict(COMPOSITE)
    )
    await proxy.delete(Position(account_id="A1", symbol="MSFT", qty=5))
    assert await _rows(db_client, "t_proxy_comp_delete", "positions") == [
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "A2", "symbol": "MSFT", "qty": 7},
    ]


async def test_composite_get_by_id_takes_a_component_mapping(db_client):
    proxy = await _service_proxy(
        db_client, "t_proxy_comp_get", "positions", Position, POSITIONS, dict(COMPOSITE)
    )
    found = await proxy.get_by_id(Position, {"account_id": "A1", "symbol": "MSFT"})
    assert found == Position(account_id="A1", symbol="MSFT", qty=5)
    missing = await proxy.get_by_id(Position, {"account_id": "A9", "symbol": "MSFT"})
    assert missing is None
    with pytest.raises(IdFieldResolutionError) as ei:
        await proxy.get_by_id(Position, "A1_MSFT")
    assert "account_id" in str(ei.value) and "symbol" in str(ei.value)


async def test_composite_delete_by_id_takes_a_component_mapping(db_client):
    proxy = await _service_proxy(
        db_client, "t_proxy_comp_del_by_id", "positions", Position, POSITIONS, dict(COMPOSITE)
    )
    await proxy.delete_by_id(Position, {"account_id": "A2", "symbol": "MSFT"})
    assert await _rows(db_client, "t_proxy_comp_del_by_id", "positions") == [
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "A1", "symbol": "MSFT", "qty": 5},
    ]


async def test_single_key_rows_unchanged_through_the_real_service(db_client):
    """Drift lock the FakeDBClient cases cannot give: a single-field table's
    mutations, applied by the real service, land on the same rows as today."""
    widgets = [{"widget_id": "W1", "status": "new"}, {"widget_id": "W2", "status": "new"}]
    proxy = await _service_proxy(
        db_client, "t_proxy_single_key", "widgets", WidgetLike, widgets, {"widgets": "widget_id"}
    )
    await proxy.update(WidgetLike(widget_id="W1", status="ready"))
    await proxy.delete(WidgetLike(widget_id="W2"))
    assert await _rows(db_client, "t_proxy_single_key", "widgets") == [
        {"widget_id": "W1", "status": "ready"}
    ]
