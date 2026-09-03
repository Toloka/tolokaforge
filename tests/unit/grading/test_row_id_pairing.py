"""Surrogate-id-aware row pairing contract for the runner-side state diff.

Locks the invariant: two rows that share ANY id-suffixed field with equal
values pair as ``different`` (a row whose surrogate ``id`` was reassigned
by the substrate), not as a ``missing`` + ``extra`` pair. Prior behaviour
returned False on the first non-null id-field mismatch, which fabricated a
false diff whenever the alphabetically-first shared id-field was the
substrate-generated surrogate.

Sibling to ``test_hash_verdict_parity``: both lock the runner-side diff's
verdict shape, this one on the row-pairing branch.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.runner.grading import _records_might_match, compute_state_diff

pytestmark = pytest.mark.unit


def test_surrogate_id_swap_pairs_via_shared_business_id() -> None:
    """A row whose ``id`` was reassigned but whose ``order_id`` is stable
    pairs as ``different``, not as a missing+extra false diff.

    The prior implementation checked shared id-suffixed fields in sorted
    order and returned on the first non-null pair. Here the sorted order
    is ``["id", "order_id"]``; ``id`` differs (1 vs 42) and the old code
    returned False, so the two rows landed in ``missing`` and ``extra``.
    Correct behaviour: keep iterating and accept the ``order_id`` match.
    """
    golden = {"id": 1, "order_id": "ord-A", "amount": 100}
    trial = {"id": 42, "order_id": "ord-A", "amount": 100}

    assert _records_might_match(golden, trial) is True


def test_surrogate_id_swap_via_compute_state_diff_reports_different_not_missing() -> None:
    """End-to-end: same business row with a swapped surrogate ``id`` shows
    up as one ``different`` entry with a field diff on ``id`` — not as
    ``1 missing, 1 extra``."""
    golden = {"orders": [{"id": 1, "order_id": "ord-A", "amount": 100}]}
    trial = {"orders": [{"id": 42, "order_id": "ord-A", "amount": 100}]}

    diff = compute_state_diff(trial, golden)

    assert diff.identical is False
    orders = diff.tables.get("orders")
    assert orders is not None, f"expected 'orders' in tables, got {sorted(diff.tables)}"
    assert len(orders.missing) == 0, f"expected 0 missing, got {orders.missing}"
    assert len(orders.extra) == 0, f"expected 0 extra, got {orders.extra}"
    assert len(orders.different) == 1, f"expected 1 different, got {orders.different}"
    assert "different" in diff.summary
    assert "missing" not in diff.summary or "0 missing" in diff.summary


def test_null_id_field_skipped_not_treated_as_mismatch() -> None:
    """When one side's id-suffixed field is ``None``, that field carries no
    identity signal and the pairing continues to the next id-suffixed field
    rather than treating null-vs-value as a match or a mismatch."""
    golden = {"id": None, "order_id": "ord-A"}
    trial = {"id": 42, "order_id": "ord-A"}

    assert _records_might_match(golden, trial) is True


def test_all_id_fields_mismatch_falls_through_to_50pct_threshold() -> None:
    """When every shared id-suffixed field mismatches, the fallback threshold
    (≥ 50% of common fields' values equal) still decides. Locks the fallback
    surface so the iterate-every-id fix does not accidentally drop it."""
    golden = {"id": 1, "customer_id": "cust-X", "amount": 100, "note": "same"}
    trial = {"id": 42, "customer_id": "cust-Y", "amount": 100, "note": "same"}

    # id and customer_id both mismatch → id-field pairing returns False.
    # Common fields: {id, customer_id, amount, note}. Equal: {amount, note} = 2.
    # 2 >= 4 * 0.5 = 2.0 → threshold holds → True.
    assert _records_might_match(golden, trial) is True


def test_all_id_fields_mismatch_below_threshold_returns_false() -> None:
    """Correct rejection: when id-fields mismatch AND fewer than half the
    common fields' values match, the two rows do not pair."""
    golden = {"id": 1, "customer_id": "cust-X", "amount": 100, "note": "alpha"}
    trial = {"id": 42, "customer_id": "cust-Y", "amount": 999, "note": "beta"}

    # No id-field match; 0 of 4 common fields' values equal → 0 < 2.0 → False.
    assert _records_might_match(golden, trial) is False


@pytest.mark.parametrize(
    "id_field",
    ["id", "order_id", "customer_id", "sku_id", "lot_id"],
)
def test_any_shared_id_field_can_carry_the_match(id_field: str) -> None:
    """The pairing recognises every ``_id``-suffixed field name equally; no
    hardcoded list of business-domain id names."""
    golden: dict[str, Any] = {"id": 1, id_field: "shared"}
    trial: dict[str, Any] = {"id": 42, id_field: "shared"}

    assert _records_might_match(golden, trial) is True
