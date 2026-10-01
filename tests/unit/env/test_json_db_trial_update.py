"""``POST /trials/{trial_id}/update`` applies a JSONPath op batch to one trial, atomically.

A batch commits only when every op succeeds and the result is still a map of
table name to a list of row objects. A refused batch leaves the rows, the
version and the SQL mirror untouched, and an update never reaches another trial.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tolokaforge.runner.db_client import (
    InvalidOperationError,
    TrialNotFoundError,
    ValidationError,
)

pytestmark = pytest.mark.unit

TICKETS = {"tickets": [{"id": "T-100", "status": "open"}], "audit_log": []}
TWO_TICKETS = {
    "tickets": [{"id": "T-100", "status": "open"}, {"id": "T-200", "status": "new"}],
    "audit_log": [],
}


def _new_trial(client, tables: dict) -> str:
    trial_id = f"update:{uuid4().hex[:8]}"
    resp = client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert resp.status_code == 200, resp.text
    return trial_id


def _update(client, trial_id: str, ops: list[dict]):
    return client.post(f"/trials/{trial_id}/update", json={"ops": ops})


def _state(client, trial_id: str) -> dict:
    resp = client.get(f"/trials/{trial_id}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sql(client, trial_id: str, query: str) -> list[dict]:
    resp = client.post(f"/trials/{trial_id}/sql", json={"query": query})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def _sql_count(client, trial_id: str, table: str) -> int:
    return _sql(client, trial_id, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def test_replace_lands_in_trial_state_and_bumps_version(db_test_client):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(
        db_test_client,
        trial_id,
        [{"op": "replace", "path": "$.tickets[0].status", "value": "closed"}],
    )

    assert resp.status_code == 200, resp.text
    after = _state(db_test_client, trial_id)
    assert after["data"]["tickets"] == [{"id": "T-100", "status": "closed"}]
    assert after["version"] == before["version"] + 1
    assert resp.json() == {
        "status": "ok",
        "version": after["version"],
        "stable_hash": after["stable_hash"],
    }


def test_batch_with_a_failing_second_op_applies_nothing(db_test_client):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)
    append_row = {"op": "add", "path": "$.tickets.-", "value": {"id": "T-200", "status": "new"}}

    resp = _update(
        db_test_client,
        trial_id,
        [append_row, {"op": "replace", "path": "$.tickets[5].status", "value": "x"}],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert detail["details"]["op_index"] == 1
    assert "op 1" in detail["message"]
    after = _state(db_test_client, trial_id)
    assert after["data"] == before["data"]
    assert after["version"] == before["version"]
    assert _sql_count(db_test_client, trial_id, "tickets") == 1

    # Control: the first op alone does change rows and the SQL mirror.
    assert _update(db_test_client, trial_id, [append_row]).status_code == 200
    assert _sql_count(db_test_client, trial_id, "tickets") == 2


@pytest.mark.parametrize(
    ("op", "tickets"),
    [
        pytest.param(
            {"op": "remove", "path": "$.tickets[?(@.id=='T-100')]"},
            [{"id": "T-200", "status": "new"}],
            id="remove-a-row-by-filter",
        ),
        pytest.param(
            {"op": "remove", "path": "$.tickets[0].status"},
            [{"id": "T-100"}, {"id": "T-200", "status": "new"}],
            id="remove-a-field",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[0].note", "value": "vip"},
            [{"id": "T-100", "status": "open", "note": "vip"}, {"id": "T-200", "status": "new"}],
            id="add-a-key-on-a-row",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets.-", "value": {"id": "T-300"}},
            [
                {"id": "T-100", "status": "open"},
                {"id": "T-200", "status": "new"},
                {"id": "T-300"},
            ],
            id="add-appends-to-a-table",
        ),
    ],
)
def test_a_write_op_lands_in_the_state_and_the_sql_mirror(db_test_client, op, tickets):
    trial_id = _new_trial(db_test_client, TWO_TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [op])

    assert resp.status_code == 200, resp.text
    after = _state(db_test_client, trial_id)
    assert after["data"]["tickets"] == tickets
    assert after["version"] == before["version"] + 1
    mirrored = _sql(db_test_client, trial_id, "SELECT * FROM tickets ORDER BY id")
    columns = {key for row in tickets for key in row}
    assert mirrored == [{column: row.get(column) for column in columns} for row in tickets]


def test_remove_of_a_path_matching_nothing_changes_no_row_and_still_bumps_the_version(
    db_test_client,
):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [{"op": "remove", "path": "$.tickets[9]"}])

    assert resp.status_code == 200, resp.text
    after = _state(db_test_client, trial_id)
    assert after["data"] == before["data"]
    assert after["stable_hash"] == before["stable_hash"]
    assert after["version"] == before["version"] + 1


@pytest.mark.parametrize(
    ("op", "message_part"),
    [
        pytest.param(
            {"op": "add", "path": "$.ticket[0].note", "value": 1},
            "add path '$.ticket[0].note' has a parent that matches nothing",
            id="add-parent-matches-nothing",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[0].status.note", "value": 1},
            "add path '$.tickets[0].status.note' has a parent holding a str",
            id="add-onto-a-scalar",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[0]", "value": []},
            "add path '$.tickets[0]' does not end in a key name",
            id="add-ending-in-an-index",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[?(@.id=='T-100')]", "value": {}},
            "does not end in a key name",
            id="add-ending-in-a-filter",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[0].*", "value": 1},
            "add path '$.tickets[0].*' does not end in a key name",
            id="add-ending-in-a-wildcard",
        ),
        pytest.param(
            {"op": "add", "path": "$", "value": []},
            "add path '$' does not end in a key name",
            id="add-on-the-root",
        ),
        pytest.param(
            {"op": "replace", "path": "$", "value": {"tickets": []}},
            "replace path '$' addresses the root '$'",
            id="replace-the-root",
        ),
        pytest.param(
            {"op": "remove", "path": "$"},
            "remove path '$' addresses the root '$'",
            id="remove-the-root",
        ),
    ],
)
def test_an_op_that_would_write_nothing_it_names_is_refused(db_test_client, op, message_part):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [op])

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert message_part in detail["message"]
    assert detail["details"] == {"op_index": 0, "op": op["op"], "path": op["path"]}
    if "does not end in a key name" in message_part:
        assert "e.g. '$.tickets.-'" in detail["message"]
    assert _state(db_test_client, trial_id) == before


@pytest.mark.parametrize(
    ("op", "message_part"),
    [
        pytest.param(
            {"op": "replace", "path": "$.tickets[0].status", "value": 10**30},
            "row 0 field 'status' holds 1000000000000000000000000000000",
            id="an-integer-sqlite-cannot-hold",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[0].Status", "value": "x"},
            "duplicate column name: Status",
            id="a-key-colliding-with-another-in-sql",
        ),
    ],
)
def test_a_batch_the_sql_mirror_cannot_store_is_refused_and_leaves_the_trial_intact(
    db_test_client, op, message_part
):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [op])

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert "table 'tickets'" in detail["message"]
    assert message_part in detail["message"]
    assert _state(db_test_client, trial_id) == before
    assert _sql(db_test_client, trial_id, "SELECT * FROM tickets") == TICKETS["tickets"]

    # Control: the trial still takes a storable write, mirror included.
    close = {"op": "replace", "path": "$.tickets[0].status", "value": "closed"}
    assert _update(db_test_client, trial_id, [close]).status_code == 200
    assert _sql(db_test_client, trial_id, "SELECT status FROM tickets") == [{"status": "closed"}]


def test_a_key_holding_a_double_quote_is_mirrored_to_sql(db_test_client):
    trial_id = _new_trial(db_test_client, TICKETS)

    resp = _update(
        db_test_client, trial_id, [{"op": "add", "path": '$.tickets[0].a"b', "value": 1}]
    )

    assert resp.status_code == 200, resp.text
    assert _sql(db_test_client, trial_id, 'SELECT "a""b" AS quoted FROM tickets') == [{"quoted": 1}]


@pytest.mark.parametrize(
    ("op", "reason"),
    [
        pytest.param({"op": "add", "path": "$.foo", "value": "x"}, "'foo'", id="non-list-table"),
        pytest.param(
            {"op": "add", "path": "$.tickets.-", "value": "x"},
            "'tickets'",
            id="non-object-row",
        ),
    ],
)
def test_batch_leaving_a_non_table_shape_is_refused(db_test_client, op, reason):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [op])

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "InvalidOperation"
    assert detail["details"]["op_index"] == 0
    assert reason in detail["message"]
    assert "list of row objects" in detail["message"]
    assert _state(db_test_client, trial_id) == before


@pytest.mark.parametrize(
    ("op", "error", "message_part"),
    [
        pytest.param(
            {"op": "replace", "path": "/tickets/0/status", "value": "closed"},
            "InvalidJSONPath",
            "is not a JSONPath",
            id="json-pointer",
        ),
        pytest.param(
            {"op": "replace", "path": "$.tickets[", "value": "closed"},
            "InvalidJSONPath",
            "not a valid JSONPath",
            id="unparseable",
        ),
        pytest.param(
            {"op": "add", "path": "$.tickets[.note", "value": 1},
            "InvalidJSONPath",
            "path '$.tickets[.note' is not a valid JSONPath",
            id="add-names-the-path-it-was-sent",
        ),
        pytest.param(
            {"op": "move", "path": "$.tickets[0]"},
            "InvalidOperation",
            "unknown op 'move'",
            id="unknown-op",
        ),
    ],
)
def test_client_caused_op_faults_are_refused_400(db_test_client, op, error, message_part):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [op])

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == error
    assert message_part in detail["message"]
    if error == "InvalidJSONPath":
        assert "e.g. '$.tickets[0].status'" in detail["message"]
    assert detail["details"]["op_index"] == 0
    assert _state(db_test_client, trial_id) == before


def test_op_missing_its_op_field_is_refused_422(db_test_client):
    trial_id = _new_trial(db_test_client, TICKETS)
    before = _state(db_test_client, trial_id)

    resp = _update(db_test_client, trial_id, [{"path": "$.x"}])

    assert resp.status_code == 422, resp.text
    locs = [entry["loc"] for entry in resp.json()["detail"]]
    assert ["body", "ops", 0, "op"] in locs
    assert _state(db_test_client, trial_id) == before


def test_update_on_one_trial_never_changes_another(db_test_client):
    trial_a = _new_trial(db_test_client, TICKETS)
    trial_b = _new_trial(db_test_client, TICKETS)
    b_before = _state(db_test_client, trial_b)

    resp = _update(
        db_test_client,
        trial_a,
        [{"op": "replace", "path": "$.tickets[0].status", "value": "closed"}],
    )

    assert resp.status_code == 200, resp.text
    assert _state(db_test_client, trial_b) == b_before


def test_update_on_an_unknown_trial_is_404(db_test_client):
    resp = _update(db_test_client, "update:never-inited", [{"op": "remove", "path": "$.tickets"}])

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "TrialNotFound"


async def test_client_update_returns_the_committed_version(db_client):
    trial_id = f"client:{uuid4().hex[:8]}"
    await db_client.init_trial(trial_id, TICKETS)

    response = await db_client.update(
        trial_id, [{"op": "replace", "path": "$.tickets[0].status", "value": "closed"}]
    )

    state = await db_client.get_state(trial_id)
    assert state.data["tickets"] == [{"id": "T-100", "status": "closed"}]
    assert response.version == state.version
    assert response.stable_hash == state.stable_hash


async def test_client_update_raises_typed_errors(db_client):
    trial_id = f"client:{uuid4().hex[:8]}"
    await db_client.init_trial(trial_id, TICKETS)

    with pytest.raises(TrialNotFoundError):
        await db_client.update("client:never-inited", [{"op": "remove", "path": "$.tickets"}])
    with pytest.raises(InvalidOperationError, match="op 0") as refused_op:
        await db_client.update(trial_id, [{"op": "replace", "path": "$.nope", "value": 1}])
    assert refused_op.value.details == {"op_index": 0, "op": "replace", "path": "$.nope"}
    with pytest.raises(ValidationError, match="JSONPath"):
        await db_client.update(trial_id, [{"op": "replace", "path": "/tickets/0", "value": 1}])
    with pytest.raises(ValidationError, match=r"ops\.0\.op: Field required"):
        await db_client.update(trial_id, [{"path": "$.x"}])
