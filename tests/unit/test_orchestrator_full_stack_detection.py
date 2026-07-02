"""Orchestrator must switch to ``full_stack`` for tasks that talk to
mock-web or rag-service (#125).

``core_stack`` only starts ``db-service`` + ``runner``. Tasks that resolve
``http://mock-web:8080/...`` (mobile/browser) or
``http://tolokaforge-rag-service:8001/...`` (search_kb) need ``full_stack``,
which adds the two extra services on top. Detection mirrors
``_tasks_need_playwright``: scan ``task.tools.agent.enabled`` AND look for
``initial_state.mock_web`` / ``initial_state.rag`` declarations.

Adapters whose search signal is not visible in task tool names (e.g. a
domain-shipped ``docindex/`` knowledge base surfaced as
``TaskDescription.search.enabled``) declare the rag-service need via
``DockerStackRequirements.needs_rag_service``; the run-level decision
(:func:`_run_needs_full_stack`) combines both signals.
"""

from __future__ import annotations

import pytest

from tolokaforge.adapters.base import DockerStackRequirements
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.orchestrator import _run_needs_full_stack, _tasks_need_full_stack

pytestmark = pytest.mark.unit


def _task(
    enabled_tools: list[str] | None = None,
    initial_state: dict | None = None,
) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        initial_state=initial_state or {},
        tools={"agent": {"enabled": enabled_tools or []}, "user": {"enabled": []}},
        user_simulator={"mode": "scripted"},
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


def test_mixed_tasks_one_full_stack_tool_triggers():
    tasks = [_task(["bash"]), _task(["search_kb", "read_file"])]
    assert _tasks_need_full_stack(tasks) is True


def test_no_full_stack_signal_returns_false():
    assert _tasks_need_full_stack([_task(["bash", "calculator", "read_file"])]) is False


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
