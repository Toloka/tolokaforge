"""A rag-service stand-in: the two per-trial endpoints the engine calls, in memory.

The runner indexes and searches through :class:`~tolokaforge.runner.rag_client.RAGServiceClient`
(async ``httpx``); the judge searches through
:class:`~tolokaforge.core.grading.kb_search.RagServiceKnowledgeSearch` (a sync
``httpx.post``). :class:`FakeRagService` answers both from one in-memory store keyed by
trial id, so a test drives the real client and the real judge search — their request
shapes, response parsing and error handling — against one index, with no container.

Ranking is word overlap, highest first, ties by ``doc_id``; zero-overlap documents are
dropped, as rag-service drops zero scores.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from tolokaforge.runner.rag_client import RAGServiceClient

BASE_URL = "http://fake-rag-service:8001"

_TRIAL_PATH = re.compile(r"^/trials/(?P<trial_id>[^/]+)/(?P<operation>index|search)$")


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"\W+", text.lower()) if token}


class FakeRagService:
    """``POST /trials/{id}/index`` and ``POST /trials/{id}/search`` over one store."""

    def __init__(self) -> None:
        self.indexes: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.searches_fail = False
        """Answer every search with a 500, as a rag-service that fell over does."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        match = _TRIAL_PATH.match(request.url.path)
        if request.method != "POST" or match is None:
            return httpx.Response(404, json={"detail": "not found"}, request=request)
        trial_id, operation = match["trial_id"], match["operation"]
        body = json.loads(request.content)
        self.requests.append((operation, trial_id, body))
        if operation == "index":
            self.indexes[trial_id] = body["documents"]
            return httpx.Response(
                200,
                json={
                    "status": "indexed",
                    "trial_id": trial_id,
                    "domain_name": body["domain_name"],
                    "documents_indexed": len(body["documents"]),
                    "index_id": f"index-{trial_id}",
                },
                request=request,
            )
        if self.searches_fail:
            return httpx.Response(500, json={"detail": "search backend down"}, request=request)
        documents = self.indexes.get(trial_id)
        if documents is None:
            return httpx.Response(
                404, json={"detail": f"no index for trial {trial_id}"}, request=request
            )
        results = self._rank(documents, body["query"], body["top_k"])
        return httpx.Response(
            200,
            json={
                "results": results,
                "query": body["query"],
                "trial_id": trial_id,
                "total_results": len(results),
            },
            request=request,
        )

    def _rank(
        self, documents: list[dict[str, Any]], query: str, top_k: int
    ) -> list[dict[str, Any]]:
        wanted = _tokens(query)
        scored = [(len(wanted & _tokens(doc["text"])), doc) for doc in documents]
        ranked = sorted(
            (row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]["doc_id"])
        )
        return [
            {
                "doc_id": doc["doc_id"],
                "text": doc["text"],
                "source": doc["source"],
                "score": float(score),
                "retrieval_method": "bm25",
            }
            for score, doc in ranked[:top_k]
        ]

    def client(self) -> RAGServiceClient:
        """The runner's client, its transport answered by this store."""
        return _MockTransportRagClient(httpx.MockTransport(self.handle))

    def route_judge_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Answer the judge's synchronous ``httpx.post`` from this store too."""

        def post(url: str, *, json: Any = None, timeout: Any = None) -> httpx.Response:
            return self.handle(httpx.Request("POST", url, json=json))

        monkeypatch.setattr(httpx, "post", post)


class _MockTransportRagClient(RAGServiceClient):
    """:class:`RAGServiceClient` with only its transport replaced."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__(base_url=BASE_URL, timeout=12.0)
        self._transport = transport

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout, transport=self._transport
            )
        return self._client
