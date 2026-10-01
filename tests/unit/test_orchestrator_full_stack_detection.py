"""Orchestrator must switch to ``full_stack`` for tasks that talk to
mock-web or rag-service (#125).

``core_stack`` only starts ``db-service`` + ``runner``. Tasks that resolve
``http://mock-web:8080/...`` (mobile/browser) or
``http://tolokaforge-rag-service:8001/...`` (a search backend declaring the
rag-service stack service — the default ``rag_service``) need ``full_stack``,
which adds the two extra services on top. Detection mirrors
``_tasks_need_playwright``: scan ``task.tools.agent.enabled``, look for
``initial_state.mock_web``, and ask the task's declared search backend — for a
task that declares a corpus or enables its search tool — which stack service it
needs (ADR-0052). A backend that runs in the runner alone keeps the core stack
whatever its tool is called.

Adapters whose search signal is not visible in task tool names (e.g. a
domain-shipped ``docindex/`` knowledge base surfaced as
``TaskDescription.search.enabled``) declare the rag-service need via
``DockerStackRequirements.needs_rag_service``; the run-level decision
(:func:`_run_needs_full_stack`) combines both signals.
"""

from __future__ import annotations

import pytest

from tests.utils.search_backends import register_search_backends
from tolokaforge.adapters.base import DockerStackRequirements
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.orchestrator import _run_needs_full_stack, _tasks_need_full_stack
from tolokaforge.testing.search_backends import in_memory_search_backend_factory

pytestmark = pytest.mark.unit


def _task(
    enabled_tools: list[str] | None = None,
    initial_state: dict | None = None,
    user_tools: list[str] | None = None,
) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        initial_state=initial_state or {},
        tools={
            "agent": {"enabled": enabled_tools or []},
            "user": {"enabled": user_tools or []},
        },
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


def test_browser_tool_triggers_full_stack():
    assert _tasks_need_full_stack([_task(["browser"])]) is True


def test_mobile_tool_triggers_full_stack():
    assert _tasks_need_full_stack([_task(["mobile"])]) is True


def test_search_kb_tool_triggers_full_stack():
    assert _tasks_need_full_stack([_task(["search_kb"])]) is True


def test_initial_state_mock_web_triggers_full_stack():
    assert (
        _tasks_need_full_stack(
            [_task(initial_state={"mock_web": {"base_url": "http://mock-web:8080"}})]
        )
        is True
    )


def test_initial_state_rag_triggers_full_stack():
    assert (
        _tasks_need_full_stack([_task(initial_state={"rag": {"corpus_dir": "rag/corpus"}})]) is True
    )


def test_an_empty_initial_state_rag_does_not_trigger_full_stack():
    """``rag: {}`` declares no corpus: a typed block is truthy, the corpus is what counts."""
    assert _tasks_need_full_stack([_task(initial_state={"rag": {}})]) is False


def test_a_renamed_search_tool_on_the_default_backend_triggers_full_stack():
    task = _task(["lookup_docs"], initial_state={"rag": {"tool": {"name": "lookup_docs"}}})
    assert _tasks_need_full_stack([task]) is True


def test_a_rag_block_that_searches_nothing_does_not_trigger_full_stack():
    """No corpus and no enabled search tool: the backend would serve nothing."""
    task = _task(["bash"], initial_state={"rag": {"backend": "rag_service"}})
    assert _tasks_need_full_stack([task]) is False


class TestTheBuiltInBm25BackendKeepsTheCoreStack:
    """``bm25`` runs in the runner process and declares no stack service."""

    def test_its_corpus_and_search_kb_tool_keep_the_core_stack(self):
        rag = {"corpus_dir": "kb", "backend": "bm25"}
        assert _tasks_need_full_stack([_task(["search_kb"], initial_state={"rag": rag})]) is False

    def test_a_configured_bm25_task_keeps_the_core_stack(self):
        rag = {
            "corpus_dir": "kb",
            "backend": "bm25",
            "backend_config": {"render": {"kind": "text"}, "ranking": {"top_k": 3}},
            "tool": {"name": "search_docs"},
        }
        assert _tasks_need_full_stack([_task(["search_docs"], initial_state={"rag": rag})]) is False

    def test_a_rag_service_task_beside_it_still_needs_the_full_stack(self):
        bm25 = _task(["search_kb"], initial_state={"rag": {"backend": "bm25", "corpus_dir": "kb"}})
        assert _tasks_need_full_stack([bm25, _task(["search_kb"])]) is True


class TestABackendWithoutAStackService:
    """The backend's declaration selects the stack; a tool named ``search_kb`` does not."""

    @pytest.fixture(autouse=True)
    def _in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        register_search_backends(monkeypatch, in_memory=in_memory_search_backend_factory)

    def test_its_search_kb_tool_keeps_the_core_stack(self):
        task = _task(["search_kb"], initial_state={"rag": {"backend": "in_memory"}})
        assert _tasks_need_full_stack([task]) is False

    def test_its_corpus_keeps_the_core_stack(self):
        rag = {"corpus_dir": "kb", "backend": "in_memory", "tool": {"name": "lookup_docs"}}
        task = _task(["lookup_docs"], initial_state={"rag": rag})
        assert _tasks_need_full_stack([task]) is False

    def test_a_default_backend_task_beside_it_still_triggers_full_stack(self):
        in_memory = _task(["search_kb"], initial_state={"rag": {"backend": "in_memory"}})
        assert _tasks_need_full_stack([in_memory, _task(["search_kb"])]) is True


def test_mixed_tasks_one_full_stack_tool_triggers():
    tasks = [_task(["bash"]), _task(["search_kb", "read_file"])]
    assert _tasks_need_full_stack(tasks) is True


def test_no_full_stack_signal_returns_false():
    assert _tasks_need_full_stack([_task(["bash", "calculator", "read_file"])]) is False


def test_a_user_declared_browser_triggers_full_stack():
    """The user simulator's tools run in the same container the agent's do.

    A ``browser`` the user declares reaches mock-web exactly as the agent's does,
    so reading ``tools.agent`` alone would start a core stack with no mock-web and
    the tool would fail at its first call.
    """
    assert _tasks_need_full_stack([_task(user_tools=["browser"])]) is True
    assert _tasks_need_full_stack([_task(user_tools=[])]) is False


def test_empty_task_list_returns_false():
    assert _tasks_need_full_stack([]) is False


def test_adapter_declared_rag_need_triggers_full_stack():
    """The task-level signals see nothing (plain tools), but the adapter
    declares search-enabled TaskDescriptions - the run must get the stack
    that actually provisions rag-service."""
    reqs = DockerStackRequirements(needs_rag_service=True)
    assert _run_needs_full_stack([_task(["bash"])], reqs) is True


def test_default_requirements_do_not_trigger_full_stack():
    assert _run_needs_full_stack([_task(["bash"])], DockerStackRequirements()) is False


def test_none_requirements_fall_back_to_task_signals():
    assert _run_needs_full_stack([_task(["bash"])], None) is False
    assert _run_needs_full_stack([_task(["search_kb"])], None) is True


def test_task_signals_still_trigger_with_default_requirements():
    assert _run_needs_full_stack([_task(["search_kb"])], DockerStackRequirements()) is True


def test_needs_rag_service_not_rendered_into_stack_kwargs():
    """``needs_rag_service`` selects the stack factory; it must NOT leak into
    ``core_stack(**kwargs)`` / ``full_stack(**kwargs)`` - neither factory
    accepts it, so a leak would TypeError at stack construction."""
    reqs = DockerStackRequirements(needs_rag_service=True)
    assert reqs.to_core_stack_kwargs() == {}
