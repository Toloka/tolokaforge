"""Unit tests for mobile tool configuration and Appium action support."""

import pytest

from tolokaforge.tools.builtin.browser import BrowserTool
from tolokaforge.tools.builtin.mobile import MobileTool
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


def _mobile_tool_for_unit_test() -> MobileTool:
    tool = MobileTool.__new__(MobileTool)
    tool.apps = {"AppOne": "http://example.com"}
    return tool


def test_execute_allows_multiple_actions(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_browser_execute(self, actions):
        captured["actions"] = actions
        return ToolResult(success=True, output="ok")

    monkeypatch.setattr(BrowserTool, "execute", _fake_browser_execute)
    tool = _mobile_tool_for_unit_test()

    result = tool.execute(
        [
            {"type": "click_at", "x": 500, "y": 500},
            {"type": "wait_5_seconds"},
        ]
    )

    assert result.success is True
    assert "actions" in captured
    assert isinstance(captured["actions"], list)
    assert len(captured["actions"]) == 2


def test_execute_rejects_empty_action_list():
    tool = _mobile_tool_for_unit_test()
    result = tool.execute([])
    assert result.success is False
    assert result.error is not None
    assert "At least one action is required" in result.error
