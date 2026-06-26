"""Behavior tests for the rag-service ``KnowledgeSearch`` impl.

The core regression these guard: the judge's KB search must hit the PER-TRIAL
``/trials/{trial_id}/search`` endpoint (the same index the agent searched), NOT
the legacy global ``/search`` the old builtin tool used.
"""

from __future__ import annotations

import httpx
import pytest

from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch, SearchHit
from tolokaforge.runner.rag_client import RAGServiceClient

pytestmark = pytest.mark.unit


def _client_with_transport(handler) -> RAGServiceClient:
    """A RAGServiceClient whose base_url/timeout drive the impl (sync httpx is patched)."""
    return RAGServiceClient(base_url="http://rag-service:8001", timeout=12.0)


def test_rag_kb_search_hits_per_trial_endpoint(monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "doc_id": "d1",
                        "text": "Refund within 30 days.",
                        "source": "policy.md",
                        "score": 0.91,
                        "retrieval_method": "hybrid",
                    }
                ],
                "query": "refund",
                "trial_id": "trial_abc:0",
                "total_results": 1,
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    kb = RagServiceKnowledgeSearch(_client_with_transport(None), "trial_abc:0")
    hits = kb.search("refund policy", top_k=3, alpha=0.25)

    # PER-TRIAL endpoint — not the global /search.
    assert captured["url"] == "http://rag-service:8001/trials/trial_abc:0/search"
    assert "/trials/" in captured["url"] and not captured["url"].endswith("8001/search")
    assert captured["json"] == {"query": "refund policy", "top_k": 3, "alpha": 0.25}
    assert captured["timeout"] == 12.0
    assert hits == [
        SearchHit(doc_id="d1", source="policy.md", score=0.91, text="Refund within 30 days.")
    ]


def test_rag_kb_search_fails_loud_on_transport_error(monkeypatch):
    def boom(url, json, timeout):
        raise httpx.ConnectError("name or service not known")

    monkeypatch.setattr(httpx, "post", boom)

    kb = RagServiceKnowledgeSearch(_client_with_transport(None), "t:0")
    # Fail loud (AGENTS.md #1) — never degrade a failed search into empty results.
    with pytest.raises(httpx.HTTPError):
        kb.search("anything")


def test_rag_kb_search_accepts_legacy_list_shape(monkeypatch):
    """Robust to a bare-list response, without ever hitting the global path."""

    def fake_post(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "doc_id": "d2",
                    "text": "Body.",
                    "source": "s.md",
                    "score": 0.5,
                    "retrieval_method": "bm25",
                }
            ],
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    kb = RagServiceKnowledgeSearch(_client_with_transport(None), "t:0")
    hits = kb.search("q")
    assert hits[0].doc_id == "d2"
