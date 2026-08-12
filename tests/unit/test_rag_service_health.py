"""``/health`` reports the semantic backend the rag-service actually has.

A rag-service whose embedding model failed to load still answered every gate
with ``200 {"status": "healthy"}`` — the Docker HEALTHCHECK, ``compose up
--wait``, the testcontainers wait strategy and ``RAGClient.is_healthy()`` all
passed while the service silently served BM25-only results under a semantic
contract. These pin the three-way rule: absent by build is healthy, installed
and loaded is healthy, installed and unloaded is 503 naming the model that
failed and why.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tolokaforge.env.rag_service import app as rag_app

pytestmark = pytest.mark.unit

_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest.mark.parametrize(
    (
        "semantic_backend_installed",
        "model_loaded",
        "expected_status_code",
        "expected_status",
        "expects_reason",
    ),
    [
        (False, False, 200, "healthy", False),
        (False, True, 200, "healthy", False),
        (True, True, 200, "healthy", False),
        (True, False, 503, "degraded", True),
    ],
)
def test_health_verdict_over_backend_and_model(
    semantic_backend_installed: bool,
    model_loaded: bool,
    expected_status_code: int,
    expected_status: str,
    expects_reason: bool,
) -> None:
    verdict = rag_app.evaluate_health(
        semantic_backend_installed=semantic_backend_installed,
        model_loaded=model_loaded,
        model_name=_MODEL,
        load_error="offline: cannot reach huggingface.co",
    )

    assert (verdict.status_code, verdict.status) == (expected_status_code, expected_status)
    assert (verdict.reason is not None) is expects_reason, (
        f"a {verdict.status} verdict carries the wrong reason shape: reason={verdict.reason!r}, "
        f"expected {'a reason naming the model' if expects_reason else 'none'}"
    )


def test_degraded_verdict_names_the_model_and_the_failure() -> None:
    verdict = rag_app.evaluate_health(
        semantic_backend_installed=True,
        model_loaded=False,
        model_name="sentence-transformers/some-other-model",
        load_error="offline: cannot reach huggingface.co",
    )

    assert verdict.reason is not None
    assert "sentence-transformers/some-other-model" in verdict.reason
    assert "offline: cannot reach huggingface.co" in verdict.reason


def test_degraded_verdict_without_a_recorded_error_is_refused() -> None:
    with pytest.raises(ValueError, match="no load error was recorded"):
        rag_app.evaluate_health(
            semantic_backend_installed=True,
            model_loaded=False,
            model_name=_MODEL,
            load_error=None,
        )


def test_state_retains_the_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_offline(model_name: str):
        raise OSError(f"cannot reach huggingface.co for {model_name}")

    monkeypatch.setattr(rag_app, "FAISS_AVAILABLE", True)
    monkeypatch.setattr(rag_app, "SentenceTransformer", raise_offline, raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "sentence-transformers/some-other-model")

    state = rag_app.RAGServiceState()

    assert state.embedding_model is None
    assert state.embedding_model_name == "sentence-transformers/some-other-model"
    assert state.embedding_load_error == (
        "cannot reach huggingface.co for sentence-transformers/some-other-model"
    )


def test_state_records_the_default_model_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    assert rag_app.RAGServiceState().embedding_model_name == rag_app.DEFAULT_EMBEDDING_MODEL


def test_health_route_reports_installed_but_unloaded_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rag_app, "FAISS_AVAILABLE", True)
    monkeypatch.setattr(rag_app.state, "embedding_model", None)
    monkeypatch.setattr(
        rag_app.state, "embedding_model_name", "sentence-transformers/some-other-model"
    )
    monkeypatch.setattr(
        rag_app.state, "embedding_load_error", "offline: cannot reach huggingface.co"
    )

    response = TestClient(rag_app.app).get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["faiss_available"] is False
    assert "sentence-transformers/some-other-model" in body["reason"]
    assert "offline: cannot reach huggingface.co" in body["reason"]


def test_health_route_reports_a_bm25_only_build_as_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rag_app, "FAISS_AVAILABLE", False)
    monkeypatch.setattr(rag_app.state, "embedding_model", None)
    monkeypatch.setattr(rag_app.state, "embedding_load_error", None)

    response = TestClient(rag_app.app).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["faiss_available"] is False
    assert body["reason"] is None


def test_health_route_reports_a_loaded_backend_as_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rag_app, "FAISS_AVAILABLE", True)
    monkeypatch.setattr(rag_app.state, "embedding_model", object())
    monkeypatch.setattr(rag_app.state, "embedding_load_error", None)

    response = TestClient(rag_app.app).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["faiss_available"] is True
    assert body["reason"] is None
