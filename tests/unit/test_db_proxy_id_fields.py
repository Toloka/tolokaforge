"""Config-driven per-table id-field resolution in DBServiceProxy.

Covers the fix for the fragile ``inspect.getsource``-based key resolution that
broke upserts/deletes for tables keyed by a natural key (not the literal ``id``,
e.g. a ``widget_id`` column) whenever the model's source file was not readable at
runtime.

The proxy now resolves the key field from config (``state_checks.id_fields``)
with a literal ``"id"`` default, so behaviour is data-driven and stable across
runtimes. A fake DB client records the mutation payloads the proxy emits and
answers query/get_state from an in-memory store.
"""

import inspect
import re
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tolokaforge.runner.db_proxy import DBServiceProxy, IdFieldResolutionError

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


async def test_resolve_id_field_fails_loud_when_unconfigured():
    proxy, _ = _proxy("widgets", WidgetLike)  # no id_fields, model has no "id"
    with pytest.raises(IdFieldResolutionError) as ei:
        proxy._resolve_id_field(WidgetLike)
    msg = str(ei.value)
    assert "widgets" in msg and "id_fields" in msg  # actionable: names table + the fix


async def test_resolve_id_field_immune_to_missing_source():
    """The old resolver parsed ``inspect.getsource(get_id)``; a model whose source is
    unavailable made it raise. The config path must resolve regardless."""
    Dyn = _make_dynamic_model()
    with pytest.raises(OSError):
        inspect.getsource(Dyn.get_id)  # confirm the source is genuinely unreadable
    proxy, _ = _proxy("dyn", Dyn, id_fields={"dyn": "dyn_id"})
    assert proxy._resolve_id_field(Dyn) == "dyn_id"


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
