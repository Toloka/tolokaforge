"""End-to-end smoke test for the public search surface.

Drives ``tolokaforge.core.search`` end-to-end using *only* the engine's
public surface plus the ``typesense`` Python package directly. No
benchmark-specific imports. This pins the 0.3.0 exit criterion that a
TypeSense-backed task can run with the public engine alone.

If this test starts depending on a non-engine package, the engine has
quietly grown a benchmark coupling — fix the coupling rather than the test.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSearchSurfaceWithoutBenchmarkPackage:
    """Container → client → index → search, using only the public surface."""

    def test_index_and_search_round_trip(self):
        from tolokaforge.core.search import SearchResponse, SearchResult
        from tolokaforge.core.search.typesense_server import (
            DOCKER_AVAILABLE,
            create_typesense_server,
        )

        if not DOCKER_AVAILABLE:
            pytest.skip("Docker SDK not installed")

        import typesense

        server = create_typesense_server(
            port="auto",
            data_dir=".cache/typesense-smoke",
            container_name="tolokaforge-typesense-smoke",
        )

        try:
            started = server.start()
            if not started:
                pytest.skip("Could not start TypeSense server")

            client = typesense.Client(
                {
                    "nodes": [
                        {
                            "host": server.host,
                            "port": server.port,
                            "protocol": "http",
                        }
                    ],
                    "api_key": server.api_key,
                    "connection_timeout_seconds": 5,
                }
            )

            collection_name = "smoke_docs"
            client.collections.create(
                {
                    "name": collection_name,
                    "fields": [
                        {"name": "title", "type": "string"},
                        {"name": "body", "type": "string"},
                    ],
                }
            )

            documents = [
                {
                    "id": "doc-1",
                    "title": "Refund policy",
                    "body": "Customers may request a refund within 30 days of purchase.",
                },
                {
                    "id": "doc-2",
                    "title": "Shipping",
                    "body": "Standard shipping arrives in three to five business days.",
                },
                {
                    "id": "doc-3",
                    "title": "Returns",
                    "body": "Returned items must be unopened and accompanied by a receipt.",
                },
            ]
            for doc in documents:
                client.collections[collection_name].documents.create(doc)

            raw = client.collections[collection_name].documents.search(
                {"q": "refund", "query_by": "title,body"}
            )

            assert raw["found"] >= 1
            hit_ids = {hit["document"]["id"] for hit in raw["hits"]}
            assert "doc-1" in hit_ids

            response = SearchResponse(
                hits=[
                    SearchResult(
                        document_id=hit["document"]["id"],
                        score=float(hit.get("text_match", 0)),
                        content=hit["document"],
                        highlights={
                            h["field"]: h.get("snippets", []) for h in hit.get("highlights", [])
                        },
                    )
                    for hit in raw["hits"]
                ],
                total_hits=raw["found"],
                query="refund",
                search_time_ms=float(raw.get("search_time_ms", 0)),
            )

            assert response.total_hits == raw["found"]
            assert any(r.document_id == "doc-1" for r in response.hits)

        finally:
            server.stop()
