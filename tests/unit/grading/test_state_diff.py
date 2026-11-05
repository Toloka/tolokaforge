"""Unit tests for the judge's ``initial → final`` state-diff renderer."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.grading import state_diff
from tolokaforge.core.grading.state_diff import render_state_diff

pytestmark = pytest.mark.unit


def test_no_changes_reports_untouched_explicitly() -> None:
    state = {"orders": [{"id": 1, "status": "open"}]}
    out = render_state_diff(state, state)
    assert "No changes detected" in out
    assert out.startswith("===== STATE CHANGES")
    assert out.rstrip().endswith("===== END STATE CHANGES =====")


def test_added_row_by_declared_pk() -> None:
    initial = {"refunds": []}
    final = {"refunds": [{"id": 88, "order_id": 42, "amount": 10.0}]}
    out = render_state_diff(initial, final, primary_keys={"refunds": "id"})
    assert "refunds: 1 added" in out
    assert '"id": 88' in out
    assert "removed" not in out


def test_removed_row() -> None:
    initial = {"orders": [{"id": 1}, {"id": 2}]}
    final = {"orders": [{"id": 1}]}
    out = render_state_diff(initial, final, primary_keys={"orders": "id"})
    assert "orders: 1 removed" in out
    assert '"id": 2' in out


def test_modified_row_shows_only_changed_fields() -> None:
    initial = {"orders": [{"id": 1, "status": "pending", "total": 5}]}
    final = {"orders": [{"id": 1, "status": "shipped", "total": 5}]}
    out = render_state_diff(initial, final, primary_keys={"orders": "id"})
    assert "orders: 1 modified" in out
    assert 'status: "pending" → "shipped"' in out
    # An unchanged field must not appear in the per-field diff.
    assert "total:" not in out


def test_star_id_heuristic_when_no_declared_pk() -> None:
    initial = {"lines": [{"order_id": 1, "qty": 1}]}
    final = {"lines": [{"order_id": 1, "qty": 3}]}
    out = render_state_diff(initial, final)  # no primary_keys passed
    assert "lines: 1 modified" in out
    assert "order_id=1" in out
    assert "qty: 1 → 3" in out


def test_whole_record_fallback_when_no_usable_key() -> None:
    initial = {"notes": [{"text": "a"}]}
    final = {"notes": [{"text": "b"}]}
    out = render_state_diff(initial, final)
    # No id/_id field and no declared pk → set matching: added + removed, never modified.
    assert "1 added" in out
    assert "1 removed" in out
    assert "modified" not in out


def test_unstable_fields_are_suppressed() -> None:
    initial = {"tickets": [{"id": 1, "status": "open", "updated_at": "t0"}]}
    final = {"tickets": [{"id": 1, "status": "open", "updated_at": "t1"}]}
    out = render_state_diff(
        initial,
        final,
        primary_keys={"tickets": "id"},
        unstable_fields={("tickets", "updated_at")},
    )
    # Only the unstable field changed → treated as no change.
    assert "No changes detected" in out
    assert "updated_at" not in out


def test_unstable_field_change_does_not_mask_real_change() -> None:
    initial = {"tickets": [{"id": 1, "status": "open", "updated_at": "t0"}]}
    final = {"tickets": [{"id": 1, "status": "closed", "updated_at": "t1"}]}
    out = render_state_diff(
        initial,
        final,
        primary_keys={"tickets": "id"},
        unstable_fields={("tickets", "updated_at")},
    )
    assert 'status: "open" → "closed"' in out
    assert "updated_at" not in out


def test_unchanged_tables_are_listed() -> None:
    initial = {
        "orders": [{"id": 1, "status": "open"}],
        "customers": [{"id": 9, "name": "x"}],
    }
    final = {
        "orders": [{"id": 1, "status": "shipped"}],
        "customers": [{"id": 9, "name": "x"}],
    }
    out = render_state_diff(initial, final, primary_keys={"orders": "id", "customers": "id"})
    assert "orders: 1 modified" in out
    assert "Unchanged tables (1): customers" in out


def test_pk_flagged_unstable_still_matches_as_modified() -> None:
    """A PK that is ALSO an unstable field (id / auto_id) must still key rows.

    Regression: previously the PK was stripped as noise before matching, so an
    in-place edit rendered as add+remove and the field-level change was lost.
    """
    initial = {"tickets": [{"id": 1, "status": "open", "subject": "help"}]}
    final = {"tickets": [{"id": 1, "status": "solved", "subject": "help"}]}
    out = render_state_diff(
        initial,
        final,
        primary_keys={"tickets": "id"},
        unstable_fields={("tickets", "id")},
    )
    assert "tickets: 1 modified" in out
    assert 'status: "open" → "solved"' in out
    assert "added" not in out and "removed" not in out


def test_keyless_fallback_preserves_multiplicity() -> None:
    """Removing one of two identical keyless rows must show as a removal.

    Regression: set-based matching collapsed duplicates, hiding the removal.
    """
    initial = {"events": [{"kind": "ping"}, {"kind": "ping"}]}
    final = {"events": [{"kind": "ping"}]}
    out = render_state_diff(initial, final)
    assert "1 removed" in out
    assert "added" not in out


def test_int_and_float_pk_are_the_same_row() -> None:
    initial = {"orders": [{"id": 1, "status": "open"}]}
    final = {"orders": [{"id": 1.0, "status": "shipped"}]}
    out = render_state_diff(initial, final, primary_keys={"orders": "id"})
    assert "orders: 1 modified" in out
    assert 'status: "open" → "shipped"' in out
    assert "added" not in out and "removed" not in out


def test_composite_declared_key_matches_modified_row() -> None:
    initial = {
        "positions": [
            {"account_id": "A1", "symbol": "MSFT", "qty": 5},
            {"account_id": "A1", "symbol": "AAPL", "qty": 2},
        ]
    }
    final = {
        "positions": [
            {"account_id": "A1", "symbol": "MSFT", "qty": 7},
            {"account_id": "A1", "symbol": "AAPL", "qty": 2},
        ]
    }
    out = render_state_diff(initial, final, primary_keys={"positions": ["account_id", "symbol"]})
    assert "positions: 1 modified" in out
    assert '~ account_id="A1", symbol="MSFT": qty: 5 → 7' in out
    assert "added" not in out and "removed" not in out


def test_composite_table_without_declaration_degrades_to_add_remove() -> None:
    # Same edit as above, no declaration: account_id repeats per side so the
    # single-field heuristic finds nothing usable and whole-record matching
    # renders the qty change as a remove + add pair. Locked so the composite
    # declaration's improvement stays visible, not assumed.
    initial = {
        "positions": [
            {"account_id": "A1", "symbol": "MSFT", "qty": 5},
            {"account_id": "A1", "symbol": "AAPL", "qty": 2},
        ]
    }
    final = {
        "positions": [
            {"account_id": "A1", "symbol": "MSFT", "qty": 7},
            {"account_id": "A1", "symbol": "AAPL", "qty": 2},
        ]
    }
    out = render_state_diff(initial, final)
    assert "positions: 1 added, 1 removed" in out
    assert "modified" not in out


def test_composite_component_int_and_float_are_the_same_row() -> None:
    initial = {"lines": [{"order_id": 1, "line_no": 2, "qty": 1}]}
    final = {"lines": [{"order_id": 1.0, "line_no": 2, "qty": 3}]}
    out = render_state_diff(initial, final, primary_keys={"lines": ["order_id", "line_no"]})
    assert "lines: 1 modified" in out
    assert "qty: 1 → 3" in out
    assert "added" not in out and "removed" not in out


def test_one_element_list_key_matches_like_the_bare_string() -> None:
    initial = {"orders": [{"id": 1, "status": "open"}]}
    final = {"orders": [{"id": 1, "status": "shipped"}]}
    out = render_state_diff(initial, final, primary_keys={"orders": ["id"]})
    assert "orders: 1 modified" in out
    assert 'id=1: status: "open" → "shipped"' in out


def test_tiny_max_chars_never_exceeds_budget_grossly() -> None:
    # Pathological max_chars must not crash or wildly overshoot (budget guard).
    initial = {"t": []}
    final = {"t": [{"id": 1, "blob": "x" * 100}]}
    out = render_state_diff(initial, final, primary_keys={"t": "id"}, max_chars=10)
    assert out.rstrip().endswith("===== END STATE CHANGES =====")


def test_truncation_is_flagged_not_silent() -> None:
    initial = {"t": []}
    final = {"t": [{"id": i, "blob": "x" * 100} for i in range(50)]}
    out = render_state_diff(initial, final, primary_keys={"t": "id"}, max_chars=300)
    assert "truncated" in out
    assert out.rstrip().endswith("===== END STATE CHANGES =====")
    assert len(out) <= 300 + 200  # cap plus the flag/footer allowance


def _world(issues: int = 3000) -> dict[str, Any]:
    """A world kept as one row whose single field nests every app's tables."""
    return {
        "world": [
            {
                "id": "w",
                "state": {
                    "apps": {
                        "jira": {
                            "issues": {
                                str(i): {"key": f"IT-{i}", "assignee": None, "note": "n" * 40}
                                for i in range(issues)
                            }
                        },
                        "workplace": {
                            "orders": [{"id": f"PO-{i}", "state": "open"} for i in range(500)]
                        },
                    },
                    "clock": "2026-09-28T12:00:00Z",
                },
            }
        ]
    }


def test_a_field_too_large_to_show_is_diffed_path_by_path() -> None:
    initial, final = _world(), _world()
    apps = final["world"][0]["state"]["apps"]
    apps["jira"]["issues"]["12"]["assignee"] = "analyst"
    apps["workplace"]["orders"][7]["state"] = "cancelled"
    apps["workplace"]["orders"].append({"id": "PO-new", "state": "open"})
    del apps["jira"]["issues"]["30"]

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert "world: 1 modified" in out
    assert "truncated" not in out
    assert '  ~ id="w":' in out
    assert '      state.apps.jira.issues.12.assignee: null → "analyst"' in out
    assert '      state.apps.jira.issues.30: {"assignee": null' in out and "→ (absent)" in out
    assert '      state.apps.workplace.orders[id="PO-7"].state: "open" → "cancelled"' in out
    assert (
        '      + state.apps.workplace.orders[id="PO-new"]: {"id": "PO-new", "state": "open"}' in out
    )
    assert "clock" not in out


def test_a_large_field_is_never_serialised_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    initial, final = _world(), _world()
    final["world"][0]["state"]["apps"]["jira"]["issues"]["5"]["assignee"] = "analyst"
    whole = len(json.dumps(initial["world"][0]["state"], ensure_ascii=False))
    longest = 0
    encode = state_diff._ENCODER.iterencode

    def recording(value: Any) -> Any:
        nonlocal longest
        size = 0
        for chunk in encode(value):
            size += len(chunk)
            longest = max(longest, size)
            yield chunk

    monkeypatch.setattr(state_diff._ENCODER, "iterencode", recording)
    fmt = state_diff._fmt_value

    def fmt_recording(value: Any) -> str:
        nonlocal longest
        text = fmt(value)
        longest = max(longest, len(text))
        return text

    monkeypatch.setattr(state_diff, "_fmt_value", fmt_recording)

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert 'state.apps.jira.issues.5.assignee: null → "analyst"' in out
    assert whole > 200_000
    assert longest <= 50_000 + 1_000


def test_changed_paths_of_one_field_are_capped_with_a_count() -> None:
    initial, final = _world(), _world()
    for i in range(80):
        final["world"][0]["state"]["apps"]["jira"]["issues"][str(i)]["assignee"] = "a"

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert out.count('.assignee: null → "a"') == 50
    assert "… (30 more changed paths)" in out


def test_a_record_too_large_to_show_is_cut() -> None:
    initial: dict[str, list[dict[str, Any]]] = {"t": []}
    final = {"t": [{"id": 1, "blob": "x" * 200_000}]}

    out = render_state_diff(initial, final, primary_keys={"t": "id"})

    assert "t: 1 added" in out
    assert "… (cut)" in out
    assert "truncated" not in out


def test_a_small_nested_change_keeps_its_one_line_form() -> None:
    initial = {"orders": [{"id": 1, "address": {"city": "A", "zip": "1"}}]}
    final = {"orders": [{"id": 1, "address": {"city": "B", "zip": "1"}}]}

    out = render_state_diff(initial, final, primary_keys={"orders": "id"})

    assert '  ~ id=1: address: {"city": "A", "zip": "1"} → {"city": "B", "zip": "1"}' in out


def test_keyless_rows_too_large_to_show_still_match_as_a_multiset() -> None:
    big = {"text": "y" * 100_000}
    initial = {"notes": [big, big]}
    final = {"notes": [big]}

    out = render_state_diff(initial, final)

    assert "notes: 1 removed" in out
    assert "… (cut)" in out


def test_a_key_dropped_from_a_large_field_is_shown_absent() -> None:
    initial, final = _world(), _world()
    initial["world"][0]["state"]["apps"]["jira"]["issues"]["3"]["resolution"] = None

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert "No changes detected" not in out
    assert "      state.apps.jira.issues.3.resolution: null → (absent)" in out


def test_a_reordered_keyed_list_in_a_large_field_says_so() -> None:
    initial, final = _world(), _world()
    orders = final["world"][0]["state"]["apps"]["workplace"]["orders"]
    orders[0], orders[1] = orders[1], orders[0]

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert "      state.apps.workplace.orders: order changed" in out


def test_a_key_that_reads_as_a_path_is_quoted() -> None:
    initial, final = _world(), _world()
    initial["world"][0]["state"]["apps"]["a.b"] = {"x": 1}
    final["world"][0]["state"]["apps"]["a.b"] = {"x": 2}

    out = render_state_diff(initial, final, primary_keys={"world": "id"})

    assert '      state.apps["a.b"].x: 1 → 2' in out


def test_a_lone_surrogate_in_a_keyless_table_still_diffs() -> None:
    out = render_state_diff({"notes": [{"text": "\ud800"}]}, {"notes": []})

    assert "notes: 1 removed" in out
