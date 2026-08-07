"""Unit tests for the judge's ``initial → final`` state-diff renderer."""

from __future__ import annotations

import pytest

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
