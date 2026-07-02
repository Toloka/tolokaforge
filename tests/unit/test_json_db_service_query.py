"""Regression tests for the DB service ``/query`` JSONPath endpoint.

The rubric judge's natural first move is to look up an entity by id, e.g.
``$.orders[?(@.id=="PO-K7V3")]``. Under the base ``jsonpath_ng`` parser this
failed with "Unexpected character: ?", so the judge fell back to dumping whole
tables via ``[*]`` and filtering mentally — roughly doubling its tool calls and
inflating context (PR #151 client feedback, v0.6.0). The endpoint now uses the
extended parser, which supports filter expressions while remaining a strict
superset of the wildcard/index grammar that already worked.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _init(client, trial_id: str, tables: dict) -> None:
    resp = client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert resp.status_code == 200, resp.text


def test_query_supports_jsonpath_filter_expression(db_test_client):
    _init(
        db_test_client,
        "t_filter",
        {
            "orders": [
                {"id": "PO-K7V3", "status": "open"},
                {"id": "PO-ZZZ9", "status": "closed"},
            ]
        },
    )
    resp = db_test_client.post(
        "/trials/t_filter/query",
        json={"jsonpath": '$.orders[?(@.id=="PO-K7V3")]'},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["status"] == "open"


def test_query_filter_on_nested_field(db_test_client):
    _init(
        db_test_client,
        "t_nested",
        {"orders": [{"id": "A", "qty": 1}, {"id": "B", "qty": 5}]},
    )
    resp = db_test_client.post(
        "/trials/t_nested/query",
        json={"jsonpath": "$.orders[?(@.qty>3)].id"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == ["B"]


def test_query_wildcard_still_works(db_test_client):
    # The extended parser must not regress the wildcard/index grammar.
    _init(
        db_test_client,
        "t_wild",
        {"orders": [{"id": "A", "status": "open"}, {"id": "B", "status": "closed"}]},
    )
    resp = db_test_client.post(
        "/trials/t_wild/query",
        json={"jsonpath": "$.orders[*].status"},
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["results"]) == ["closed", "open"]
