from __future__ import annotations

import pytest

from tolokaforge.runner.tool_result import tool_error_message

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"error": "denied"}, "denied"),
        ('{"error":"denied"}', "denied"),
        (
            {"content": [{"type": "text", "text": '{"error":"denied"}'}]},
            "denied",
        ),
        (
            {"isError": True, "content": [{"type": "text", "text": "denied"}]},
            "denied",
        ),
        ({"status": "ok"}, None),
        ("ordinary text", None),
    ],
)
def test_tool_error_message(result: object, expected: str | None) -> None:
    assert tool_error_message(result) == expected
