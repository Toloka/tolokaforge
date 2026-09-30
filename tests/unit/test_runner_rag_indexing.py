"""The runner's search-index construction resolves the trial's corpus against the
extracted artifacts dir, and the ``rag_service`` backend fails loud on an empty index.

A native RAG task bundles its corpus into ``tool_artifacts`` under the declared
``corpus_dir``; the runner extracts that to ``artifacts_dir`` and must resolve a
relative ``documents_path`` as ``artifacts_dir / documents_path`` (mirroring the
mcp_server_script resolver) before handing it to the backend named by
``search.plane``. An absolute ``documents_path`` is used literally. When search is
enabled but the corpus resolves empty — the path is unresolvable or holds no
documents — ``RegisterTrial`` is refused with a ``SearchIndexBuildError`` so the
trial hard-fails rather than running against an empty index and masking a bundling
bug as an agent failure.

Drives :meth:`RunnerServiceImpl._build_search_index`, the one construction site,
through the registered ``rag_service`` backend with a recording client.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.search.backend import SearchIndexBuildError
from tolokaforge.runner.models import SearchConfig, TaskDescription
from tolokaforge.runner.rag_service_backend import RagServiceSearchIndex
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit


class _RecordingRagClient:
    """Captures the documents handed to the rag-service without any network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list]] = []

    async def index_documents(self, *, trial_id: str, domain_name: str, documents: list) -> None:
        self.calls.append((trial_id, domain_name, documents))


@pytest.fixture
def service() -> RunnerServiceImpl:
    rag_client = _RecordingRagClient()
    svc = RunnerServiceImpl(db_client=MagicMock(), rag_client=rag_client)
    yield svc
    svc.shutdown()


def _write_corpus(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "policies.md").write_text("# Policies\n\nRefund code RX-7788.\n")
    (directory / "faq.txt").write_text("Q: return window?\n")


def _index(service: RunnerServiceImpl, config: SearchConfig, artifacts_dir: Path | None) -> None:
    description = TaskDescription(
        task_id="rag_task",
        name="rag task",
        category="rag_search",
        description="rag task",
        adapter_type="native",
        system_prompt="system",
        search=config,
    )
    index = service._build_search_index("t:0", description, artifacts_dir)
    assert isinstance(index, RagServiceSearchIndex)


def test_relative_documents_path_resolves_against_artifacts_dir(
    service: RunnerServiceImpl, tmp_path: Path
) -> None:
    _write_corpus(tmp_path / "rag" / "corpus")
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/corpus"
    )

    _index(service, config, artifacts_dir=tmp_path)

    assert len(service.rag_client.calls) == 1
    trial_id, domain_name, documents = service.rag_client.calls[0]
    assert trial_id == "t:0"
    assert domain_name == "rag_search"
    # Both the .md and .txt corpus files under artifacts_dir / documents_path
    # were loaded — proving the resolution and the flat glob.
    assert len(documents) == 2


def test_absolute_documents_path_stays_literal(service: RunnerServiceImpl, tmp_path: Path) -> None:
    corpus = tmp_path / "abs_corpus"
    _write_corpus(corpus)
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path=str(corpus)
    )

    # No artifacts_dir: an absolute path must not need one.
    _index(service, config, artifacts_dir=None)

    assert len(service.rag_client.calls) == 1
    assert len(service.rag_client.calls[0][2]) == 2


def test_relative_path_without_artifacts_dir_raises(
    service: RunnerServiceImpl,
) -> None:
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/corpus"
    )

    with pytest.raises(SearchIndexBuildError, match="cannot be resolved"):
        _index(service, config, artifacts_dir=None)
    assert service.rag_client.calls == []


def test_unresolvable_dir_raises(service: RunnerServiceImpl, tmp_path: Path) -> None:
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/missing"
    )

    with pytest.raises(SearchIndexBuildError, match="no documents"):
        _index(service, config, artifacts_dir=tmp_path)
    assert service.rag_client.calls == []


def test_empty_corpus_dir_raises(service: RunnerServiceImpl, tmp_path: Path) -> None:
    (tmp_path / "rag" / "corpus").mkdir(parents=True)
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/corpus"
    )

    with pytest.raises(SearchIndexBuildError, match="no documents"):
        _index(service, config, artifacts_dir=tmp_path)
    assert service.rag_client.calls == []
