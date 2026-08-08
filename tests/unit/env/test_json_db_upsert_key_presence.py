"""The db-service upsert refuses a record that omits its resolved key field.

A single-field ``key`` (default ``id``) requires the record to **contain**
that field; an explicit ``null`` value is a legal, addressable key. Every
upsert in a batch — including one with no ``record`` at all — is validated
before any operation applies, so a refused batch leaves the rows, the
version, and the SQL mirror untouched. The matcher requires the stored row
to carry every key field, so a stored ``null`` matches an incoming ``null``
instead of the first row that lacks the field.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

POSITIONS = [
    {"account_id": "A1", "symbol": "AAPL", "qty": 10},
    {"account_id": "A2", "symbol": "MSFT", "qty": 5},
]


def _init(client, trial_id: str, tables: dict) -> None:
    resp = client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert resp.status_code == 200, resp.text


def _mutate(client, trial_id: str, table: str, operations: list[dict]):
    return client.patch(f"/trials/{trial_id}/state/{table}", json={"operations": operations})


def _state(client, trial_id: str) -> dict:
    resp = client.get(f"/trials/{trial_id}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sql_count(client, trial_id: str, table: str) -> int:
    resp = client.post(
        f"/trials/{trial_id}/sql", json={"query": f"SELECT COUNT(*) AS n FROM {table}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]["n"]


def test_upsert_lacking_its_key_field_is_refused(db_test_client):
    _init(db_test_client, "t_keyless", {"positions": POSITIONS})

    resp = _mutate(
        db_test_client,
        "t_keyless",
        "positions",
        [{"op": "upsert", "record": {"symbol": "NVDA", "qty": 3}, "key": "account_id"}],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert "positions" in detail["message"]
    assert "account_id" in detail["message"]
    assert "symbol" in detail["message"] and "qty" in detail["message"]
    assert detail["details"]["table_name"] == "positions"
    assert detail["details"]["missing_components"] == ["account_id"]
    assert detail["details"]["record_keys"] == ["qty", "symbol"]
    assert detail["details"]["op_index"] == 0
    assert _state(db_test_client, "t_keyless")["data"]["positions"] == POSITIONS


def test_omitted_key_defaults_to_id_and_requires_it(db_test_client):
    _init(db_test_client, "t_no_id", {"widgets": [{"id": "W1", "status": "new"}]})

    resp = _mutate(
        db_test_client, "t_no_id", "widgets", [{"op": "upsert", "record": {"status": "done"}}]
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert detail["details"]["missing_components"] == ["id"]
    assert detail["details"]["op_index"] == 0
    assert _state(db_test_client, "t_no_id")["data"]["widgets"] == [{"id": "W1", "status": "new"}]


def test_mid_batch_refusal_applies_no_earlier_operation(db_test_client):
    _init(db_test_client, "t_atomic", {"positions": POSITIONS})
    version_before = _state(db_test_client, "t_atomic")["version"]

    resp = _mutate(
        db_test_client,
        "t_atomic",
        "positions",
        [
            {"op": "insert", "record": {"account_id": "A9", "symbol": "IBM", "qty": 1}},
            {"op": "upsert", "record": {"symbol": "NVDA", "qty": 3}, "key": "account_id"},
        ],
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["details"]["op_index"] == 1
    state = _state(db_test_client, "t_atomic")
    assert state["data"]["positions"] == POSITIONS
    assert state["version"] == version_before
    assert _sql_count(db_test_client, "t_atomic", "positions") == len(POSITIONS)


def test_record_less_upsert_mid_batch_is_refused_without_applying(db_test_client):
    _init(db_test_client, "t_no_record", {"positions": POSITIONS})
    version_before = _state(db_test_client, "t_no_record")["version"]

    resp = _mutate(
        db_test_client,
        "t_no_record",
        "positions",
        [
            {"op": "insert", "record": {"account_id": "A9", "symbol": "IBM", "qty": 1}},
            {"op": "upsert"},
        ],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert detail["message"] == "Upsert requires 'record'"
    assert detail["details"]["op_index"] == 1
    state = _state(db_test_client, "t_no_record")
    assert state["data"]["positions"] == POSITIONS
    assert state["version"] == version_before


def test_explicit_null_key_updates_the_stored_null_row(db_test_client):
    _init(
        db_test_client,
        "t_null_key",
        {"widgets": [{"status": "keyless"}, {"id": None, "status": "stale"}]},
    )

    resp = _mutate(
        db_test_client,
        "t_null_key",
        "widgets",
        [{"op": "upsert", "record": {"id": None, "status": "updated"}}],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["affected_rows"] == 1
    assert _state(db_test_client, "t_null_key")["data"]["widgets"] == [
        {"status": "keyless"},
        {"id": None, "status": "updated"},
    ]
