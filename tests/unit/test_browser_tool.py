"""Test browser tool schema and pure-logic helpers (no real browser)."""

import pytest

from tolokaforge.tools.builtin.browser import BrowserTool

pytestmark = pytest.mark.unit


@pytest.fixture
def browser_tool():
    """Create browser tool instance (no browser launched for pure-logic tests)."""
    return BrowserTool()


def test_browser_schema(browser_tool):
    """Test that schema includes all Gemini actions"""
    schema = browser_tool.get_schema()
    actions = schema["function"]["parameters"]["properties"]["actions"]["items"]["properties"][
        "type"
    ]["enum"]

    # Verify all Gemini actions are present
    expected_actions = [
        "open_web_browser",
        "navigate",
        "wait_5_seconds",
        "screenshot",
        "go_back",
        "go_forward",
        "search",
        "click_at",
        "hover_at",
        "type_text_at",
        "select",
        "key_combination",
        "scroll_document",
        "scroll_at",
        "drag_and_drop",
    ]

    assert set(actions) == set(
        expected_actions
    ), f"Missing actions: {set(expected_actions) - set(actions)}"


def test_grid_to_pixel_conversion(browser_tool):
    """Test coordinate conversion from 1000x1000 grid to pixels"""
    # Test corners of grid
    assert browser_tool._grid_to_pixel(0, 0) == (0, 0)
    assert browser_tool._grid_to_pixel(999, 999) == (
        999 / 1000 * browser_tool.viewport_width,
        999 / 1000 * browser_tool.viewport_height,
    )

    # Test center
    pixel_x, pixel_y = browser_tool._grid_to_pixel(500, 500)
    assert abs(pixel_x - browser_tool.viewport_width / 2) < 1
    assert abs(pixel_y - browser_tool.viewport_height / 2) < 1


def test_key_alias_normalization_list(browser_tool):
    """Test list-form key aliases normalize to Playwright names."""
    assert browser_tool._normalize_key_combination(["DOWN"]) == "ArrowDown"
    assert browser_tool._normalize_key_combination(["BACKSPACE"]) == "Backspace"
    assert browser_tool._normalize_key_combination(["CTRL", "BACKSPACE"]) == "Control+Backspace"


def test_key_alias_normalization_string(browser_tool):
    """Test string-form key aliases normalize to Playwright names."""
    assert browser_tool._normalize_key_combination("DOWN") == "ArrowDown"
    assert browser_tool._normalize_key_combination("CTRL+BACKSPACE") == "Control+Backspace"


def test_empty_actions_array(browser_tool):
    """Test that empty actions array is handled"""
    result = browser_tool.execute(actions=[])
    # Should succeed but do nothing
    assert result.success


def test_invalid_action_type(browser_tool):
    """Test that invalid action types are rejected"""
    result = browser_tool.execute(
        actions=[
            {"type": "invalid_action"},
        ]
    )

    assert not result.success
    assert "Unknown action type" in result.error


def test_missing_required_parameters(browser_tool):
    """Test that missing required parameters are handled"""
    # Missing URL for navigate
    result = browser_tool.execute(
        actions=[
            {"type": "open_web_browser"},
            {"type": "navigate"},  # Missing URL
        ]
    )
    assert not result.success
