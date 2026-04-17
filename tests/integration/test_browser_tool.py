"""Integration tests for browser tool with real Chromium browser.

These tests launch a real browser via Playwright and navigate to URLs.
"""

import logging

import pytest

from tolokaforge.tools.builtin.browser import BrowserTool

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_browser]


@pytest.fixture
def browser_tool():
    """Create browser tool instance"""
    tool = BrowserTool()
    yield tool
    # Cleanup: must run on the SAME event loop where Playwright was started.
    # asyncio.run() creates a new loop which causes Playwright operations to hang.
    if tool._loop and not tool._loop.is_closed():
        try:
            tool._loop.run_until_complete(tool.cleanup())
        except Exception:  # noqa: BLE001 - Best-effort Playwright cleanup
            logger.warning("Failed to clean up Playwright browser", exc_info=True)
        tool.close_loop()
    else:
        # Fallback: no loop was created (e.g., execute() was never called)
        pass


def test_open_and_navigate(browser_tool):
    """Test browser open and navigation"""
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "navigate", "url": "https://example.com"},
        ]
    )

    assert result.success, f"Browser navigation failed: {result.error}"
    assert "example.com" in result.output.lower() or "example" in result.output.lower()


def test_coordinate_actions(browser_tool):
    """Test coordinate-based click and hover"""
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "navigate", "url": "https://example.com"},
            {"type": "hover_at", "x": 500, "y": 300},
            {"type": "click_at", "x": 500, "y": 300},
        ]
    )

    assert result.success, f"Coordinate actions failed: {result.error}"


def test_type_text_at(browser_tool):
    """Test type_text_at with modifiers"""
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "navigate", "url": "https://example.com"},
            {
                "type": "type_text_at",
                "x": 500,
                "y": 300,
                "text": "test input",
                "clear_before_typing": True,
                "press_enter": False,
            },
        ]
    )

    assert result.success, f"Type text action failed: {result.error}"


def test_safety_decision_metadata(browser_tool):
    """Test that risky actions surface safety_decision metadata"""
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "navigate", "url": "https://example.com"},
            {"type": "click_at", "x": 500, "y": 300},
        ]
    )

    assert result.success
    # Check safety_decision metadata is present
    assert "safety_decision" in result.metadata
    assert result.metadata["safety_decision"]["requires_confirmation"] is True
    assert "navigate" in result.metadata["safety_decision"]["risky_actions"]
    assert "click_at" in result.metadata["safety_decision"]["risky_actions"]


def test_safety_decision_non_risky_actions(browser_tool):
    """Test that non-risky actions don't trigger safety_decision"""
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "wait_5_seconds"},
        ]
    )

    assert result.success
    # wait_5_seconds is not risky, so no safety_decision should be present
    # (but open_web_browser is also not risky, so metadata should be empty)
    assert "safety_decision" not in result.metadata or not result.metadata
