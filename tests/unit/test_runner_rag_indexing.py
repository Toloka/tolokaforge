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

import base64
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.utils.fake_rag_service import FakeRagService
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tests.utils.search_backends import register_search_backends
from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch
from tolokaforge.core.search.backend import SearchIndexBuildError
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import SearchConfig, TaskDescription
from tolokaforge.runner.rag_client import RAGServiceClient
from tolokaforge.runner.rag_service_backend import RagServiceSearchIndex
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.testing.search_backends import InMemorySearchBackend, SearchBackendDefects

pytestmark = pytest.mark.unit


class _RecordingRagClient(RAGServiceClient):
    """The runner's client, capturing the documents it indexes without any network."""

    def __init__(self) -> None:
        super().__init__(base_url="http://rag-service:8001")
        self.calls: list[tuple[str, str, list]] = []

    async def index_documents(  # type: ignore[override]
        self, *, trial_id: str, domain_name: str, documents: list
    ) -> None:
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


# ---------------------------------------------------------------------------
# Version skew: what engines released before ``search.plane`` named a backend send.
# ---------------------------------------------------------------------------

_SKEWED_SEARCH = [
    pytest.param({"enabled": True}, id="enabled-with-no-plane"),
    pytest.param({"enabled": True, "plane": "typesense"}, id="typesense-plane-with-enabled"),
]
"""Every released engine emits ``enabled: true`` for a rag corpus and no plane (the
native adapter's before ``plane`` existed, an external adapter's still); a task
declaring the TypeSense plane with ``enabled`` has always had rag-service too."""


@pytest.mark.parametrize("skewed", _SKEWED_SEARCH)
def test_a_skewed_search_block_builds_the_rag_service_index(
    service: RunnerServiceImpl, tmp_path: Path, skewed: dict
) -> None:
    _write_corpus(tmp_path / "rag" / "corpus")
    config = SearchConfig(domain_name="rag_search", documents_path="rag/corpus", **skewed)

    _index(service, config, artifacts_dir=tmp_path)

    trial_id, domain_name, documents = service.rag_client.calls[0]
    assert (trial_id, domain_name, len(documents)) == ("t:0", "rag_search", 2)


@pytest.mark.parametrize("skewed", _SKEWED_SEARCH)
def test_register_trial_serves_a_skewed_search_kb_from_rag_service(
    skewed: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``RegisterTrial``: indexed, searched, and given to the judge."""
    monkeypatch.delenv("TYPESENSE_HOST", raising=False)
    monkeypatch.delenv("TYPESENSE_PORT", raising=False)
    rag_service = FakeRagService()
    runner = RunnerServiceImpl(db_client=MagicMock(), rag_client=rag_service.client())
    trial_id = f"skew_{skewed.get('plane', 'none')}:0"
    task = {
        "task_id": "skew",
        "name": "skew",
        "category": "rag_search",
        "description": "a search block as a released engine sends it",
        "adapter_type": "tlk_mcp_core",
        "system_prompt": "system",
        "agent_tools": [
            {
                "name": "search_kb",
                "description": "Search the knowledge base.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                "category": "read",
                "timeout_s": 15.0,
            }
        ],
        "search": {"domain_name": "rag_search", "documents_path": "rag/corpus", **skewed},
        "tool_artifacts": {
            "rag/corpus/policies.md": base64.b64encode(
                b"# Policies\n\nRefund code RX-7788.\n"
            ).decode()
        },
    }
    try:
        registered = runner.RegisterTrial(
            register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id),
            MagicMock(),
        )
        assert registered.success is True, registered.error
        assert [doc["source"] for doc in rag_service.indexes[trial_id]] == ["policies.md"]

        answer = runner.ExecuteTool(
            execute_request(trial_id, "search_kb", json.dumps({"query": "refund code"})),
            MagicMock(),
        )
        assert answer.status == pb2.EXECUTION_STATUS_SUCCESS, answer.error
        assert json.loads(answer.output)["results"][0]["source"] == "policies.md"
        assert isinstance(runner.trials[trial_id].resolve_kb_search(), RagServiceKnowledgeSearch)
    finally:
        runner.shutdown()


def test_a_build_the_loop_never_ran_is_closed_not_leaked(
    service: RunnerServiceImpl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduling failure refuses the trial and leaves no never-awaited coroutine."""
    _write_corpus(tmp_path / "rag" / "corpus")
    scheduled: list[Any] = []

    def refuse_to_schedule(coroutine: Any, timeout: float = 300.0) -> Any:
        scheduled.append(coroutine)
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(service, "_run_async", refuse_to_schedule)
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/corpus"
    )

    with pytest.raises(SearchIndexBuildError, match="Event loop is closed"):
        _index(service, config, artifacts_dir=tmp_path)
    (build,) = scheduled
    assert inspect.getcoroutinestate(build) == inspect.CORO_CLOSED


# ---------------------------------------------------------------------------
# The declared stack-service surface (``tolokaforge.core.search.stack_services``).
# ---------------------------------------------------------------------------


def test_a_rag_service_task_on_a_runner_that_does_not_reach_it_is_refused(
    tmp_path: Path,
) -> None:
    """The operator reads which service is missing and how a runner reaches it."""
    runner = RunnerServiceImpl(db_client=MagicMock())
    _write_corpus(tmp_path / "rag" / "corpus")
    config = SearchConfig(
        enabled=True, plane="rag_service", domain_name="rag_search", documents_path="rag/corpus"
    )
    try:
        with pytest.raises(SearchIndexBuildError) as excinfo:
            _index(runner, config, artifacts_dir=tmp_path)
    finally:
        runner.shutdown()

    message = str(excinfo.value)
    assert message.startswith(
        "Trial t:0: search backend 'rag_service' cannot build the trial's index: "
        "stack service 'rag_service' is not reachable from this runner: "
    )
    assert "RAG_SERVICE_URL" in message
    assert "--profile full" in message


def test_a_backend_declaring_an_undeclared_stack_service_is_refused_before_it_builds(
    service: RunnerServiceImpl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No runner reaches a service the surface does not declare, so nothing is built."""
    built: list[InMemorySearchBackend] = []

    def factory(context: Any) -> InMemorySearchBackend:
        backend = InMemorySearchBackend(
            context, defects=SearchBackendDefects(declares_an_undeclared_stack_service=True)
        )
        built.append(backend)
        return backend

    register_search_backends(monkeypatch, elsewhere=factory)
    _write_corpus(tmp_path / "rag" / "corpus")
    config = SearchConfig(plane="elsewhere", domain_name="rag_search", documents_path="rag/corpus")

    with pytest.raises(SearchIndexBuildError) as excinfo:
        _index(service, config, artifacts_dir=tmp_path)

    message = str(excinfo.value)
    assert message.startswith(
        "Trial t:0: search backend 'elsewhere' cannot build the trial's index: "
        "stack service 'undeclared_service' is not declared by this engine; "
        "the declared stack services are ['rag_service']"
    )
    assert [backend.call_log.builds for backend in built] == [[]], "build_index never ran"
