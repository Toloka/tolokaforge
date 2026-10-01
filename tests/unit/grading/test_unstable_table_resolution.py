"""One resolution of an ``unstable_fields`` table name, shared by the db-service and the view.

The db-service masks a trial's stable state by resolving each registered table name
against the trial's data tables (exact, singular / plural, suffix). A comparison view
moves step 2 of the pre-hash order onto the client, which must mask the same columns,
so the resolution lives in :mod:`tolokaforge.core.hash` and the service calls it. The
cases below pin what each strategy answers, and the service is held to them.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.hash import resolve_unstable_field_paths, resolve_unstable_table_name
from tolokaforge.env.json_db_service.app import TrialState, UnstableFieldSpec

pytestmark = pytest.mark.unit

_TABLES = frozenset(
    {"reservations", "flight", "sn_customerservice_case", "servicenow_csm_tasks", "audit"}
)

_CASES = (
    pytest.param("reservations", "reservations", id="exact"),
    pytest.param("reservation", "reservations", id="singular-names-a-plural-table"),
    pytest.param("flights", "flight", id="plural-names-a-singular-table"),
    pytest.param("csm_tasks", "servicenow_csm_tasks", id="a-table-ends-with-the-name"),
    pytest.param(
        "customerservice_cases",
        "sn_customerservice_case",
        id="a-table-ends-with-the-name-minus-s",
    ),
    pytest.param(
        "servicenow_csm_sn_customerservice_case",
        "sn_customerservice_case",
        id="the-name-ends-with-a-table",
    ),
    pytest.param(
        "servicenow_csm_sn_customerservice_cases",
        "sn_customerservice_case",
        id="the-name-minus-s-ends-with-a-table",
    ),
    pytest.param("legacy_audits", "audit", id="the-name-ends-with-a-table-plus-s"),
    pytest.param("payments", None, id="no-match"),
)


@pytest.mark.parametrize(("declared", "resolved"), _CASES)
def test_each_strategy_resolves_the_name_it_is_for(declared: str, resolved: str | None) -> None:
    assert resolve_unstable_table_name(declared, _TABLES) == resolved


def test_a_name_two_tables_match_by_suffix_resolves_to_the_first_in_sorted_order() -> None:
    """Set iteration order varies with the hash seed; the answer must not."""
    tables = ["zz_orders", "aa_orders"]
    assert resolve_unstable_table_name("orders", tables) == "aa_orders"
    assert resolve_unstable_table_name("orders", list(reversed(tables))) == "aa_orders"


def test_an_exact_name_wins_over_every_other_strategy() -> None:
    assert resolve_unstable_table_name("order", {"order", "orders", "x_order"}) == "order"


def test_paths_keep_the_declared_name_where_nothing_matches_and_keep_their_order() -> None:
    paths = ["reservation.id", "payments.created_at", "flights.updated_at", "version"]
    assert resolve_unstable_field_paths(paths, _TABLES) == [
        "reservations.id",
        "payments.created_at",
        "flight.updated_at",
        "version",
    ]


def test_a_path_splits_at_its_first_dot_as_the_filter_reads_it() -> None:
    assert resolve_unstable_field_paths(["reservation.meta.id"], _TABLES) == [
        "reservations.meta.id"
    ]


@pytest.mark.parametrize(("declared", "resolved"), _CASES)
def test_the_db_service_masks_by_the_shared_resolution(declared: str, resolved: str | None) -> None:
    """The service's own list is the shared function's answer, case by case."""
    trial = TrialState(
        trial_id="t",
        data={table: [{"id": 1}] for table in _TABLES},
        unstable_fields={
            (declared, "id"): UnstableFieldSpec(table_name=declared, field_name="id"),
        },
    )
    expected = f"{resolved or declared}.id"
    assert trial.get_unstable_field_list() == [expected]
    assert resolve_unstable_field_paths([f"{declared}.id"], _TABLES) == [expected]
