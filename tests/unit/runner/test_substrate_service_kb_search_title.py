"""Locks :meth:`SubstrateServicer.KBSearch`'s ``title`` round trip (ADR-0052 change 2).

The remote grader's judge reads the runner's knowledge search through
``KBSearch``. A backend whose documents have titles (``bm25``) hands them across
in ``SubstrateSearchHit.title``; a backend without them (rag-service) leaves the
optional field unset, and :class:`GrpcSubstrateClient` reads that back as
``None``, never as an empty string.
"""

from __future__ import annotations

from concurrent import futures
from types import SimpleNamespace

import grpc
import pytest

from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient
from tolokaforge.runner import add_SubstrateServiceServicer_to_server
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.unit

_TRIAL_ID = "kb_title_trial"
_HITS = [
    SearchHit(doc_id="d1", source="d1.json", score=2.5, text="first", title="First document"),
    SearchHit(doc_id="d2", source="d2.md", score=0.0, text="second"),
    SearchHit(doc_id="d3", source="d3.json", score=0.0, text="third", title=""),
]


class _FakeDBClient:
    async def close(self) -> None:
        return None


class _ScriptedSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        self.calls.append((query, top_k, alpha))
        return list(_HITS)


def test_titles_cross_the_wire_and_an_absent_title_stays_none() -> None:
    runner = RunnerServiceImpl(db_client=_FakeDBClient())  # type: ignore[arg-type]
    search = _ScriptedSearch()
    runner.trials[_TRIAL_ID] = SimpleNamespace(resolve_kb_search=lambda: search)  # type: ignore[assignment]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            result = GrpcSubstrateClient(channel, _TRIAL_ID).kb_search("query", 3, 0.5)
    finally:
        server.stop(grace=None)
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)

    assert search.calls == [("query", 3, 0.5)]
    assert result.kb_available is True
    assert result.hits == _HITS, "a set title, an absent one and an empty one each round-trip"
    assert result.hits[1].title is None
    assert result.hits[2].title == ""
