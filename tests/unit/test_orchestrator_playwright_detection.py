"""Orchestrator must enable Playwright for any Playwright-dependent tool,
not just ``browser`` (#110).

``MobileTool`` is a subclass of ``BrowserTool`` and equally requires the
Playwright + Chromium runtime — the orchestrator's pre-flight detection
at ``tolokaforge/core/orchestrator.py`` only checked ``"browser"``,
so any ``examples/mobile/`` run failed at trial registration with
``BrowserTool requires Playwright. Install with: pip install
'tolokaforge[browser]'`` because the runner image was built without
Playwright. Surfaced when the #110 fix unblocked the rest of the
mobile path.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import TaskConfig
from tolokaforge.core.orchestrator import _tasks_need_playwright

pytestmark = pytest.mark.unit


def _task(enabled_tools: list[str], user_tools: list[str] | None = None) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        initial_state={},
        tools={"agent": {"enabled": enabled_tools}, "user": {"enabled": user_tools or []}},
        actors={"user": {"mode": "scripted"}},
        grading="grading.yaml",
    )


def test_browser_tool_triggers_playwright():
    assert _tasks_need_playwright([_task(["browser"])]) is True


def test_mobile_tool_triggers_playwright():
    assert _tasks_need_playwright([_task(["mobile"])]) is True


def test_mixed_tasks_one_mobile_triggers_playwright():
    tasks = [_task(["bash"]), _task(["mobile", "read_file"])]
    assert _tasks_need_playwright(tasks) is True


def test_a_user_declared_mobile_tool_triggers_playwright():
    """The runner reconstructs a user tool through the same wrapper as an agent tool,
    so a user-declared Playwright tool needs the same image built for it."""
    assert _tasks_need_playwright([_task(["read_file"], user_tools=["mobile"])]) is True
    assert _tasks_need_playwright([_task(["read_file"], user_tools=[])]) is False


def test_no_playwright_tools_returns_false():
    assert _tasks_need_playwright([_task(["bash", "calculator", "read_file"])]) is False


def test_empty_task_list_returns_false():
    assert _tasks_need_playwright([]) is False
