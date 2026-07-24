"""Unit tests for :mod:`tolokaforge.runner.id_resolution`.

The shared helper backs three diff-sync paths (DB proxy for tools, Tau
in-memory sync, MCP subprocess sync). These tests exercise it directly so
failures point at the helper rather than at a caller's plumbing.
"""

import pytest

from tolokaforge.runner.id_resolution import (
    IdFieldResolutionError,
    id_field_for_table,
    resolve_record_id,
)

pytestmark = pytest.mark.unit


def test_id_field_for_table_configured_wins():
    assert id_field_for_table("widgets", {"widgets": "widget_id"}) == "widget_id"


def test_id_field_for_table_defaults_to_id():
    assert id_field_for_table("users", {}) == "id"
    assert id_field_for_table("users", {"widgets": "widget_id"}) == "id"


def test_id_field_for_table_blank_configured_falls_through_to_id():
    # ``or`` (not ``.get(..., "id")``) so blank config folds to the default,
    # matching the DB proxy's resolver.
    assert id_field_for_table("widgets", {"widgets": ""}) == "id"


def test_resolve_record_id_configured_key():
    value = resolve_record_id(
        {"widget_id": "W1", "status": "new"},
        "widgets",
        {"widgets": "widget_id"},
    )
    assert value == "W1"


def test_resolve_record_id_defaults_to_id_field():
    value = resolve_record_id({"id": "X1", "name": "a"}, "items", {})
    assert value == "X1"


def test_resolve_record_id_missing_key_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id({"widget_id": "W1"}, "widgets", {})
    # The default "id" was resolved (no config), record doesn't have it → fail loud.
    msg = str(ei.value)
    assert "widgets" in msg
    assert "'id'" in msg
    assert "widget_id" in msg  # record keys surfaced for debugging


def test_resolve_record_id_wrong_configured_key_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id({"id": "X1"}, "widgets", {"widgets": "widget_id"})
    msg = str(ei.value)
    assert "widget_id" in msg
    assert "widgets" in msg
    assert "state_checks.id_fields" in msg  # points authors at the fix


def test_resolve_record_id_falsy_value_returned_not_raised():
    # Key present with a falsy value (0, "", False) must return the value,
    # not treat the record as keyless.
    assert resolve_record_id({"id": 0, "name": "a"}, "items", {}) == 0
    assert resolve_record_id({"id": "", "name": "a"}, "items", {}) == ""
