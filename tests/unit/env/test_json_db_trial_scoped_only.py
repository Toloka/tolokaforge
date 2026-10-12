"""db-service keeps data only per trial: no flat route reaches a shared store.

A request for data must name a trial that was inited. Neither a path outside
``/trials/{trial_id}`` nor a trial id nobody inited yields a store.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/reset", {"tickets": []}),
        ("POST", "/query", {"jsonpath": "$"}),
        ("POST", "/update", {"ops": [{"op": "add", "path": "$.tickets", "value": []}]}),
        ("GET", "/dump", None),
        ("POST", "/sql", {"query": "SELECT 1"}),
        ("GET", "/schema", None),
    ],
)
def test_a_flat_data_path_is_not_served(db_test_client, method, path, body):
    resp = db_test_client.request(method, path, json=body)

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "Not Found"}


def test_query_on_a_trial_nobody_inited_is_trial_not_found(db_test_client):
    resp = db_test_client.post("/trials/scoped:never-inited/query", json={"jsonpath": "$"})

    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "TrialNotFound"
    assert detail["details"] == {"trial_id": "scoped:never-inited"}
