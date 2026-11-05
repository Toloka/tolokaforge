"""The db-service's table digests: one encoding on both sides, and an older service's absence.

A state holder's sync compares the mirror's ``GET /trials/{id}/state/digests`` with
:func:`tolokaforge.core.hash.table_digest` of its own tables, so the two must encode a
table alike — the db-service image runs its vendored copy, held to core here. A service
that predates the route answers FastAPI's bare 404 or 405, which the client reads as
"no digests" so the sync falls back to the whole state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.utils.db_service_fallback import load_standalone_fallback
from tolokaforge.core.hash import table_digest
from tolokaforge.env.json_db_service.app import app
from tolokaforge.runner.db_client import DBServiceClient, TrialNotFoundError

pytestmark = pytest.mark.unit

_VENDORED = load_standalone_fallback("table_digest")

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=6), children, max_size=4),
    max_leaves=20,
)
_TABLES = st.lists(st.dictionaries(st.text(max_size=6), _JSON, max_size=5), max_size=5)


class TestTheEncoding:
    @settings(max_examples=200, deadline=None)
    @given(rows=_TABLES)
    def test_the_db_services_copy_digests_as_core_does(self, rows: list[dict[str, Any]]) -> None:
        assert _VENDORED(rows) == table_digest(rows)

    @settings(max_examples=100, deadline=None)
    @given(rows=_TABLES)
    def test_it_is_the_hash_of_the_tables_canonical_json(self, rows: list[dict[str, Any]]) -> None:
        whole = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert table_digest(rows) == hashlib.sha256(whole.encode()).hexdigest()

    def test_an_int_and_its_float_digest_apart(self) -> None:
        """Equal in Python, different JSON: a reader must not take one for the other."""
        assert table_digest([{"n": 1}]) != table_digest([{"n": 1.0}])


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _init(client: TestClient, tables: dict[str, Any]) -> str:
    trial_id = f"digests_{uuid.uuid4().hex[:8]}:0"
    response = client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert response.status_code == 200, response.text
    return trial_id


class TestTheEndpoint:
    def test_each_table_in_the_states_order(self, client: TestClient) -> None:
        tables = {"zeta": [{"id": 1}], "alpha": [{"id": "a", "v": [1, 2]}]}
        trial_id = _init(client, tables)

        body = client.get(f"/trials/{trial_id}/state/digests").json()

        assert list(body["digests"]) == ["zeta", "alpha"]
        assert body["digests"] == {name: table_digest(rows) for name, rows in tables.items()}

    def test_the_tables_asked_for(self, client: TestClient) -> None:
        trial_id = _init(client, {"a": [{"id": 1}], "b": [], "c": [{"id": 3}]})

        body = client.get(f"/trials/{trial_id}/state/digests", params={"tables": "c,a"}).json()

        assert list(body["digests"]) == ["a", "c"]

    def test_an_unknown_trial_is_not_found(self, client: TestClient) -> None:
        response = client.get("/trials/nobody:0/state/digests")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "TrialNotFound"


class _Answering:
    """A test client that answers every GET with one canned response."""

    def __init__(self, status: int, body: Any) -> None:
        self._status, self._body = status, body

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(self._status, json=self._body, request=httpx.Request("GET", url))


class TestTheClient:
    async def test_digests_come_back_in_the_services_order(self) -> None:
        client = DBServiceClient("http://db.test:8000")
        client.set_test_client(_Answering(200, {"digests": {"b": "2", "a": "1"}, "version": 3}))

        assert list((await client.get_table_digests("t:0")) or {}) == ["b", "a"]

    @pytest.mark.parametrize(
        ("status", "detail"), [(404, "Not Found"), (405, "Method Not Allowed")]
    )
    async def test_a_service_without_the_route_gives_none(self, status: int, detail: str) -> None:
        client = DBServiceClient("http://db.test:8000")
        client.set_test_client(_Answering(status, {"detail": detail}))

        assert await client.get_table_digests("t:0") is None

    async def test_a_missing_trial_is_still_an_error(self) -> None:
        client = DBServiceClient("http://db.test:8000")
        body = {
            "detail": {"error": "TrialNotFound", "message": "no", "details": {"trial_id": "t:0"}}
        }
        client.set_test_client(_Answering(404, body))

        with pytest.raises(TrialNotFoundError):
            await client.get_table_digests("t:0")
