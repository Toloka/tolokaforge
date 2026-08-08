"""The db-service upsert matcher compares every declared key component.

An upsert's ``key`` is a single field name or an ordered list of component
names. A composite match holds only when the candidate row agrees with the
incoming record on **every** component — never on a concatenation, which
collides (``"a_b" + "c"`` vs ``"a" + "b_c"``) — and a composite upsert whose
record omits a component, or carries ``None`` for one, is refused before any
operation in the batch is applied.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

POSITIONS = [
    {"account_id": "A1", "symbol": "AAPL", "qty": 10},
    {"account_id": "A1", "symbol": "MSFT", "qty": 5},
    {"account_id": "A2", "symbol": "MSFT", "qty": 7},
]


def _init(client, trial_id: str, tables: dict) -> None:
    resp = client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert resp.status_code == 200, resp.text


def _upsert(client, trial_id: str, table: str, record: dict, **payload):
    return client.patch(
        f"/trials/{trial_id}/state/{table}",
        json={"operations": [{"op": "upsert", "record": record, **payload}]},
    )


def _rows(client, trial_id: str, table: str) -> list[dict]:
    resp = client.get(f"/trials/{trial_id}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"][table]


def test_composite_upsert_updates_only_the_fully_matching_row(db_test_client):
    _init(db_test_client, "t_comp_update", {"positions": POSITIONS})

    resp = _upsert(
        db_test_client,
        "t_comp_update",
        "positions",
        {"account_id": "A1", "symbol": "MSFT", "qty": 99},
        key=["account_id", "symbol"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["affected_rows"] == 1
    assert _rows(db_test_client, "t_comp_update", "positions") == [
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "A1", "symbol": "MSFT", "qty": 99},
        {"account_id": "A2", "symbol": "MSFT", "qty": 7},
    ]


def test_composite_upsert_inserts_when_no_row_matches_every_component(db_test_client):
    _init(db_test_client, "t_comp_insert", {"positions": POSITIONS})

    resp = _upsert(
        db_test_client,
        "t_comp_insert",
        "positions",
        {"account_id": "A1", "symbol": "TSLA", "qty": 1},
        key=["account_id", "symbol"],
    )

    assert resp.status_code == 200, resp.text
    assert _rows(db_test_client, "t_comp_insert", "positions") == POSITIONS + [
        {"account_id": "A1", "symbol": "TSLA", "qty": 1}
    ]


def test_composite_match_is_per_component_never_a_concatenation(db_test_client):
    # Under any joined-string matcher these two rows share the key "x_y_z".
    _init(
        db_test_client,
        "t_comp_concat",
        {"pairs": [{"a": "x_y", "b": "z", "v": 1}, {"a": "x", "b": "y_z", "v": 2}]},
    )

    resp = _upsert(
        db_test_client,
        "t_comp_concat",
        "pairs",
        {"a": "x", "b": "y_z", "v": 9},
        key=["a", "b"],
    )

    assert resp.status_code == 200, resp.text
    assert _rows(db_test_client, "t_comp_concat", "pairs") == [
        {"a": "x_y", "b": "z", "v": 1},
        {"a": "x", "b": "y_z", "v": 9},
    ]


def test_composite_upsert_missing_component_is_refused(db_test_client):
    _init(db_test_client, "t_comp_missing", {"positions": POSITIONS})

    resp = _upsert(
        db_test_client,
        "t_comp_missing",
        "positions",
        {"account_id": "A1", "qty": 99},
        key=["account_id", "symbol"],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert "positions" in detail["message"]
    assert "symbol" in detail["message"]
    assert "account_id" in detail["message"] and "qty" in detail["message"]
    assert detail["details"]["table_name"] == "positions"
    assert detail["details"]["missing_components"] == ["symbol"]
    assert detail["details"]["record_keys"] == ["account_id", "qty"]
    assert detail["details"]["op_index"] == 0
    assert _rows(db_test_client, "t_comp_missing", "positions") == POSITIONS


def test_composite_upsert_null_component_is_refused(db_test_client):
    # A row seeded without `symbol` must not be reachable through an explicit
    # null component: None == None would merge into the row that carries no
    # `symbol` at all — the composite variant of the keyless collapse.
    seeded = [{"account_id": "A1", "qty": 10}]
    _init(db_test_client, "t_comp_null", {"positions": seeded})

    resp = _upsert(
        db_test_client,
        "t_comp_null",
        "positions",
        {"account_id": "A1", "symbol": None, "qty": 99},
        key=["account_id", "symbol"],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert detail["details"]["table_name"] == "positions"
    assert detail["details"]["missing_components"] == ["symbol"]
    assert detail["details"]["op_index"] == 0
    assert _rows(db_test_client, "t_comp_null", "positions") == seeded


def test_empty_key_list_is_refused(db_test_client):
    _init(db_test_client, "t_comp_empty", {"positions": POSITIONS})

    resp = _upsert(
        db_test_client,
        "t_comp_empty",
        "positions",
        {"account_id": "A1", "symbol": "MSFT", "qty": 99},
        key=[],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert "positions" in detail["message"]
    assert detail["details"]["op_index"] == 0
    assert _rows(db_test_client, "t_comp_empty", "positions") == POSITIONS


def test_string_key_upsert_is_unchanged(db_test_client):
    _init(
        db_test_client,
        "t_str_key",
        {"widgets": [{"id": "W1", "status": "new"}, {"id": "W2", "status": "new"}]},
    )

    resp = _upsert(db_test_client, "t_str_key", "widgets", {"id": "W2", "status": "done"}, key="id")

    assert resp.status_code == 200, resp.text
    assert _rows(db_test_client, "t_str_key", "widgets") == [
        {"id": "W1", "status": "new"},
        {"id": "W2", "status": "done"},
    ]


def test_omitted_key_still_defaults_to_id(db_test_client):
    _init(
        db_test_client,
        "t_no_key",
        {"widgets": [{"id": "W1", "status": "new"}, {"id": "W2", "status": "new"}]},
    )

    resp = _upsert(db_test_client, "t_no_key", "widgets", {"id": "W1", "status": "done"})

    assert resp.status_code == 200, resp.text
    assert _rows(db_test_client, "t_no_key", "widgets") == [
        {"id": "W1", "status": "done"},
        {"id": "W2", "status": "new"},
    ]
