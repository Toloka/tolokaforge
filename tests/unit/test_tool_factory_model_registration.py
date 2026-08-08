"""Model-to-table registration reads the declared ``state_checks.id_fields`` map.

``ToolFactory._register_toolset_models`` matches each toolset model class to a
db-service table. Its declared-key strategy resolves every seeded table's key
from ``id_fields`` and claims the first table whose key components the model
declares as fields, whose first seeded record carries them, and whose first
seeded record validates against the model — explicitly declared tables ahead of
``"id"``-defaulted ones.

Every assertion reads the factory's eager registration map
(``_async_proxy._model_name_to_table``) directly, before any proxy call, so the
proxy's lazy class-name fallback cannot mask a registration miss.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import tolokaforge
from tolokaforge.runner.tool_factory import ToolFactory

pytestmark = pytest.mark.unit


def _install_toolset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, src: str) -> str:
    """Write an importable ``<name>.models`` package and return the toolset path."""
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(textwrap.dedent(src))
    monkeypatch.syspath_prepend(str(tmp_path))
    for module in (name, f"{name}.models"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    importlib.invalidate_caches()
    return name


def _register(
    toolset: str,
    *,
    db_table_names: list[str],
    initial_state_data: dict[str, list[dict[str, Any]]],
    id_fields: dict[str, str | list[str]] | None = None,
) -> dict[str, str]:
    factory = ToolFactory(
        db_client=MagicMock(),
        trial_id="trial:0",
        db_table_names=db_table_names,
        initial_state_data=initial_state_data,
        id_fields=id_fields,
    )
    factory._register_toolset_models(toolset)
    return dict(factory._async_proxy._model_name_to_table)


def test_composite_declared_key_registers_eagerly(tmp_path, monkeypatch):
    """A composite ``get_id`` returns no single field name; the declaration does."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_composite",
        """
        from pydantic import BaseModel

        class Position(BaseModel):
            account_id: str
            symbol: str
            quantity: int

            def get_id(self) -> str:
                return f"{self.account_id}_{self.symbol}"
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["positions"],
        initial_state_data={"positions": [{"account_id": "A1", "symbol": "MSFT", "quantity": 10}]},
        id_fields={"positions": ["account_id", "symbol"]},
    )

    assert registered == {"ts_composite.models.Position": "positions"}


def test_declared_key_matches_by_membership_not_position(tmp_path, monkeypatch):
    """The declared key is the *second* key of the seeded record; position is irrelevant."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_membership",
        """
        from pydantic import BaseModel

        class Sku(BaseModel):
            name: str
            sku_id: str

            def get_id(self) -> str:
                return self.sku_id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["product_skus"],
        initial_state_data={"product_skus": [{"name": "Widget", "sku_id": "S1"}]},
        id_fields={"product_skus": "sku_id"},
    )

    assert registered == {"ts_membership.models.Sku": "product_skus"}


def test_model_without_readable_source_registers_eagerly(tmp_path, monkeypatch):
    """An ``exec``-defined model has no source on disk; matching must not need it."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_dynamic",
        """
        from pydantic import BaseModel

        _namespace: dict = {"BaseModel": BaseModel, "__name__": __name__}
        exec(
            "class Ticket(BaseModel):\\n"
            "    ticket_id: str\\n"
            "    subject: str\\n"
            "    def get_id(self) -> str:\\n"
            "        return self.ticket_id\\n",
            _namespace,
        )
        Ticket = _namespace["Ticket"]
        """,
    )
    models = importlib.import_module(f"{toolset}.models")
    with pytest.raises(OSError):
        inspect.getsource(models.Ticket.get_id)  # the source is genuinely unreadable

    registered = _register(
        toolset,
        db_table_names=["support_tickets"],
        initial_state_data={"support_tickets": [{"ticket_id": "T1", "subject": "Broken"}]},
        id_fields={"support_tickets": "ticket_id"},
    )

    assert registered == {"ts_dynamic.models.Ticket": "support_tickets"}


def test_declared_single_key_in_first_position_registers(tmp_path, monkeypatch):
    """Parity: the shape the declared-key strategy shares with a plain single key."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_declared_parity",
        """
        from pydantic import BaseModel

        class Review(BaseModel):
            review_id: str
            rating: int

            def get_id(self) -> str:
                return self.review_id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["review_api_reviews"],
        initial_state_data={"review_api_reviews": [{"review_id": "R1", "rating": 5}]},
        id_fields={"review_api_reviews": "review_id"},
    )

    assert registered == {"ts_declared_parity.models.Review": "review_api_reviews"}


def test_undeclared_id_keyed_table_registers_without_a_declaration(tmp_path, monkeypatch):
    """Parity: an ``id``-keyed domain declares nothing and still registers eagerly."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_default_parity",
        """
        from pydantic import BaseModel

        class Order(BaseModel):
            id: str
            total: float

            def get_id(self) -> str:
                return self.id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["oms_orders"],
        initial_state_data={"oms_orders": [{"id": "O1", "total": 9.5}]},
        id_fields={},
    )

    assert registered == {"ts_default_parity.models.Order": "oms_orders"}


def test_declared_table_outranks_an_earlier_default_keyed_table(tmp_path, monkeypatch):
    """Key carrier: both tables validate, so only the declared rank decides the claim."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_steal_guard",
        """
        from pydantic import BaseModel

        class Review(BaseModel):
            id: str
            review_id: str
            rating: int

            def get_id(self) -> str:
                return self.review_id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["orders", "reviews"],
        initial_state_data={
            "orders": [{"id": "O1", "review_id": "R9", "rating": 5}],
            "reviews": [{"review_id": "R1", "id": "X1", "rating": 4}],
        },
        id_fields={"reviews": "review_id"},
    )

    assert registered == {"ts_steal_guard.models.Review": "reviews"}


def test_declared_table_that_fails_validation_yields_to_the_default_table(tmp_path, monkeypatch):
    """FK carrier: the declared table is tried first and rejected by ``model_validate``."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_validation_gate",
        """
        from pydantic import BaseModel

        class Order(BaseModel):
            id: str
            review_id: str
            total: float

            def get_id(self) -> str:
                return self.id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["orders", "reviews"],
        initial_state_data={
            "orders": [{"id": "O1", "review_id": "R1", "total": 9.5}],
            "reviews": [{"review_id": "R1", "rating": 4}],
        },
        id_fields={"reviews": "review_id"},
    )

    assert registered == {"ts_validation_gate.models.Order": "orders"}


def test_model_covering_no_declared_key_is_skipped_without_raising(tmp_path, monkeypatch):
    """No candidate table: the pass warns and skips, and reconstruction continues."""
    toolset = _install_toolset(
        tmp_path,
        monkeypatch,
        "ts_no_match",
        """
        from pydantic import BaseModel

        class Widget(BaseModel):
            widget_id: str
            label: str

            def get_id(self) -> str:
                return self.widget_id
        """,
    )

    registered = _register(
        toolset,
        db_table_names=["orders"],
        initial_state_data={"orders": [{"id": "O1", "total": 9.5}]},
        id_fields={},
    )

    assert registered == {}


def test_the_shipped_package_never_reads_model_source():
    """Registration is driven by declared config; the source-parsing helper is gone."""
    root = Path(tolokaforge.__file__).parent
    offenders = sorted(
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "_get_id_field_name" in line or "getsource" in line
    )

    assert offenders == []
