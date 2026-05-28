"""Orchestrator must switch to ``full_stack`` for tasks that talk to
mock-web or rag-service (#125).

``core_stack`` only starts ``db-service`` + ``runner``. Tasks that resolve
``http://mock-web:8080/...`` (mobile/browser) or
``http://tolokaforge-rag-service:8001/...`` (search_kb) need ``full_stack``,
which adds the two extra services on top. Detection mirrors
``_tasks_need_playwright``: scan ``task.tools.agent.enabled`` AND look for
``initial_state.mock_web`` / ``initial_state.rag`` declarations.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import TaskConfig
from tolokaforge.core.orchestrator import _tasks_need_full_stack

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
