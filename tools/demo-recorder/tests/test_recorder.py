"""Tests for demo_recorder.recorder — pure utility functions."""

from __future__ import annotations

import pytest
from demo_recorder.recorder import (
    expand_actions_payload,
    format_action_summary,
    grid_to_pixel,
    normalize_actions_payload,
    normalize_url,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# grid_to_pixel
# ---------------------------------------------------------------------------


class TestGridToPixel:
    def test_origin(self) -> None:
        assert grid_to_pixel(0, 0) == (0, 0)

    def test_full_grid(self) -> None:
        """Grid coordinates of (1000, 1000) map to phone dimensions (412, 915)."""
        px, py = grid_to_pixel(1000, 1000)
        assert px == 412
        assert py == 915

    def test_center(self) -> None:
        px, py = grid_to_pixel(500, 500)
        assert px == 206  # int(0.5 * 412)
        assert py == 457  # int(0.5 * 915) = 457.5 → 457


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------


class TestNormalizeUrl:
    def test_replaces_mock_web_port(self) -> None:
        assert normalize_url("http://mock-web:8080/app") == "http://localhost:8080/app"

    def test_replaces_mock_web_hostname(self) -> None:
        assert normalize_url("http://mock-web/path") == "http://localhost/path"

    def test_no_change_for_normal_url(self) -> None:
        url = "http://localhost:8080/page"
        assert normalize_url(url) == url


# ---------------------------------------------------------------------------
# normalize_actions_payload
# ---------------------------------------------------------------------------


class TestNormalizeActionsPayload:
    def test_list_passthrough(self) -> None:
        actions = [{"type": "click_at", "x": 100, "y": 200}]
        assert normalize_actions_payload(actions) == actions

    def test_json_string(self) -> None:
        payload = '[{"type": "click_at", "x": 100}]'
        result = normalize_actions_payload(payload)
        assert result == [{"type": "click_at", "x": 100}]

    def test_json_string_with_actions_key(self) -> None:
        payload = '{"actions": [{"type": "scroll_document"}]}'
        result = normalize_actions_payload(payload)
        assert result == [{"type": "scroll_document"}]

    def test_empty_string(self) -> None:
        assert normalize_actions_payload("") == []

    def test_string_with_comments(self) -> None:
        """JSON-like strings with JS comments are cleaned."""
        payload = '// click here\n[{"type": "click_at"}]'
        result = normalize_actions_payload(payload)
        assert result == [{"type": "click_at"}]

    def test_invalid_json_string(self) -> None:
        assert normalize_actions_payload("not json at all") == []

    def test_non_list_non_string(self) -> None:
        assert normalize_actions_payload(42) == []
        assert normalize_actions_payload(None) == []


# ---------------------------------------------------------------------------
# expand_actions_payload
# ---------------------------------------------------------------------------


class TestExpandActionsPayload:
    def test_flat_list(self) -> None:
        actions = [{"type": "click_at"}, {"type": "scroll_document"}]
        result = expand_actions_payload(actions)
        assert len(result) == 2

    def test_nested_actions_key(self) -> None:
        """Entries with nested 'actions' list are recursively expanded."""
        payload = [{"actions": [{"type": "click_at"}, {"type": "go_back"}]}]
        result = expand_actions_payload(payload)
        assert len(result) == 2
        assert result[0]["type"] == "click_at"
        assert result[1]["type"] == "go_back"

    def test_string_json_entry(self) -> None:
        """String entries containing JSON are parsed and expanded."""
        payload = ['[{"type": "click_at"}]']
        result = expand_actions_payload(payload)
        assert len(result) == 1
        assert result[0]["type"] == "click_at"

    def test_pipe_separated_string(self) -> None:
        """Pipe-separated strings are split into raw_action entries."""
        payload = ["tap 100 200 | scroll down"]
        result = expand_actions_payload(payload)
        assert len(result) == 2
        assert result[0]["type"] == "raw_action"
        assert result[0]["text"] == "tap 100 200"
        assert result[1]["text"] == "scroll down"

    def test_empty_list(self) -> None:
        assert expand_actions_payload([]) == []

    def test_empty_strings_skipped(self) -> None:
        assert expand_actions_payload([""]) == []
        assert expand_actions_payload(["  "]) == []


# ---------------------------------------------------------------------------
# format_action_summary
# ---------------------------------------------------------------------------


class TestFormatActionSummary:
    def test_click_at(self) -> None:
        action = {"type": "click_at", "x": 100, "y": 200}
        assert format_action_summary(action) == "tap(100, 200)"

    def test_type_text_at(self) -> None:
        action = {"type": "type_text_at", "text": "hello", "x": 50, "y": 60}
        assert format_action_summary(action) == 'type "hello" at (50, 60)'

    def test_type_text_at_long_text_truncated(self) -> None:
        action = {"type": "type_text_at", "text": "a" * 100, "x": 0, "y": 0}
        summary = format_action_summary(action)
        assert len(summary) < 120
        assert "..." in summary

    def test_scroll_document(self) -> None:
        assert format_action_summary({"type": "scroll_document", "direction": "up"}) == "scroll up"

    def test_scroll_at(self) -> None:
        action = {"type": "scroll_at", "direction": "down", "x": 50, "y": 60}
        assert format_action_summary(action) == "scroll down at (50, 60)"

    def test_open_app(self) -> None:
        assert format_action_summary({"type": "open_app", "app_name": "Maps"}) == "open Maps"

    def test_select(self) -> None:
        assert format_action_summary({"type": "select", "x": 10, "y": 20}) == "select(10, 20)"

    def test_press_enter(self) -> None:
        assert format_action_summary({"type": "press_enter"}) == "press enter"

    def test_go_back(self) -> None:
        assert format_action_summary({"type": "go_back"}) == "back"

    def test_wait(self) -> None:
        assert format_action_summary({"type": "wait_5_seconds"}) == "wait 5s"

    def test_key_combination(self) -> None:
        action = {"type": "key_combination", "keys": ["CTRL", "A"]}
        assert format_action_summary(action) == "keys ['CTRL', 'A']"

    def test_drag_and_drop(self) -> None:
        action = {
            "type": "drag_and_drop",
            "x": 10,
            "y": 20,
            "destination_x": 30,
            "destination_y": 40,
        }
        assert format_action_summary(action) == "drag (10,20) -> (30,40)"

    def test_unknown_type(self) -> None:
        assert format_action_summary({"type": "custom_action"}) == "custom_action"

    def test_raw_action(self) -> None:
        assert format_action_summary({"type": "raw_action", "text": "tap here"}) == "tap here"

    def test_string_input(self) -> None:
        assert format_action_summary("  some action  ") == "some action"

    def test_non_dict_non_string(self) -> None:
        assert format_action_summary(42) == "unknown action"
