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


class TestAMutationTouchesItsTable:
    def test_only_the_mutated_table_is_re_mirrored_into_sql(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tolokaforge.env.json_db_service import app as service

        trial_id = _init(client, {"a": [{"id": 1, "v": 1}], "b": [{"id": 2, "v": 2}]})
        mirrored: list[str] = []
        real = service.TrialState._mirror_table

        def recording(self: Any, cursor: Any, table_name: str, rows: Any) -> None:
            mirrored.append(table_name)
            real(self, cursor, table_name, rows)

        monkeypatch.setattr(service.TrialState, "_mirror_table", recording)
        ops = {"operations": [{"op": "upsert", "key": "id", "record": {"id": 1, "v": 9}}]}

        response = client.patch(f"/trials/{trial_id}/state/a", json=ops)

        assert response.status_code == 200, response.text
        assert mirrored == ["a"]
        rows = client.post(f"/trials/{trial_id}/sql", json={"query": "SELECT id, v FROM a"}).json()
        assert rows["results"] == [{"id": 1, "v": 9}]
        rows = client.post(f"/trials/{trial_id}/sql", json={"query": "SELECT id, v FROM b"}).json()
        assert rows["results"] == [{"id": 2, "v": 2}]

    def test_a_mutation_without_hashes_answers_an_empty_hash(self, client: TestClient) -> None:
        trial_id = _init(client, {"a": [{"id": 1}]})
        ops = {"operations": [{"op": "insert", "record": {"id": 2}}]}

        plain = client.patch(f"/trials/{trial_id}/state/a", json=ops, params={"hashes": "false"})
        hashed = client.patch(
            f"/trials/{trial_id}/state/a",
            json={"operations": [{"op": "insert", "record": {"id": 3}}]},
        )

        assert plain.json()["new_hash"] == ""
        assert hashed.json()["new_hash"]


class TestReplace:
    def test_a_replace_makes_the_table_exactly_its_rows(self, client: TestClient) -> None:
        trial_id = _init(client, {"a": [{"id": 1, "v": 1, "gone": True}, {"id": 2}]})
        rows = [{"id": 0}, {"id": 1, "v": 2}]

        answer = client.patch(
            f"/trials/{trial_id}/state/a", json={"operations": [{"op": "replace", "records": rows}]}
        )

        assert answer.status_code == 200, answer.text
        state = client.get(f"/trials/{trial_id}/state").json()["data"]
        assert state["a"] == rows
        sql = client.post(f"/trials/{trial_id}/sql", json={"query": "SELECT id FROM a"}).json()
        assert sql["results"] == [{"id": 0}, {"id": 1}]
        digests = client.get(f"/trials/{trial_id}/state/digests").json()["digests"]
        assert digests["a"] == table_digest(rows)

    def test_a_replace_creates_a_missing_table_even_empty(self, client: TestClient) -> None:
        trial_id = _init(client, {"a": [{"id": 1}]})

        answer = client.patch(
            f"/trials/{trial_id}/state/links",
            json={"operations": [{"op": "replace", "records": []}]},
        )

        assert answer.status_code == 200, answer.text
        assert client.get(f"/trials/{trial_id}/state").json()["data"]["links"] == []
