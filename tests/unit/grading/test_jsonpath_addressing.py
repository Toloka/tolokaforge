"""What a ``state_checks`` assertion addresses, and what a task provisions.

The two questions are separate on purpose. ``path:`` is not a synonym for
"DB-targeting" — a ``$.filesystem`` root resolves against state only the core engine
composes — and "this task provisions a database" is not ``bool(tables)``: a task
declaring only ``schemas`` or only ``unstable_fields`` gets a DB service too.
"""

from collections.abc import Mapping
from itertools import product
from typing import Any

import pytest

from tolokaforge.core.grading.jsonpath_addressing import (
    JsonPathTarget,
    addresses_the_database,
    addresses_the_filesystem,
    block_addresses_the_database,
    jsonpath_target,
)
from tolokaforge.runner.models import (
    RunnerInitialStateConfig,
    TableSchema,
    UnstableFieldSpec,
    provisions_database,
)

pytestmark = pytest.mark.unit

_A_TABLE = {"orders": [{"id": "O1", "status": "pending"}]}
_A_SCHEMA = [TableSchema(table_name="orders", fields={"id": "string", "status": "string"})]
_AN_UNSTABLE_FIELD = [UnstableFieldSpec(table_name="orders", field_name="created_at")]
_A_PROVISIONED_FILE = {
    "/env/fs/agent-visible/buggy_math.py": "def divide(a, b):\n    return a / b\n"
}

_A_DATABASE_ASSERTION = {"path": "$.db.orders[0].status", "equals": "shipped"}
_A_FILESYSTEM_ASSERTION = {
    "path": "$.filesystem['/env/fs/agent-visible/buggy_math.py']",
    "contains": "except (ValueError, TypeError)",
}
_A_PROBE = {
    "name": "orders_shipped",
    "dsn": "postgresql://grader@app-db:5432/app",
    "query": "SELECT status FROM orders WHERE id = 'O1'",
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.db.orders[0].status", JsonPathTarget.DATABASE),
        ('$["db"].orders', JsonPathTarget.DATABASE),
        ("$.tables.widgets[0].status", JsonPathTarget.DATABASE),
        ("$.filesystem['/env/fs/agent-visible/x.py']", JsonPathTarget.FILESYSTEM),
        ("$..status", JsonPathTarget.DATABASE),
        ("$.*", JsonPathTarget.DATABASE),
        ("$", JsonPathTarget.DATABASE),
        # A field named for the filesystem, under the database root: what a
        # substring reading of the written expression gets wrong, and what a
        # reading of any segment rather than the first gets wrong.
        ("$.db.notes[0].filesystem_path", JsonPathTarget.DATABASE),
        ("$.db.notes[0].filesystem", JsonPathTarget.DATABASE),
        # The root is still the first segment where the author leaves ``$`` off,
        # which both evaluators accept.
        ("filesystem['/env/fs/agent-visible/x.py']", JsonPathTarget.FILESYSTEM),
    ],
)
def test_jsonpath_target_reads_the_first_segment_below_the_root(
    path: str, expected: JsonPathTarget
) -> None:
    assert jsonpath_target(path) is expected


def test_an_unparseable_path_is_refused_by_name() -> None:
    with pytest.raises(ValueError) as raised:
        jsonpath_target("$.db[[")
    assert "$.db[[" in str(raised.value)


@pytest.mark.parametrize(
    ("assertion", "reads_the_database", "reads_the_filesystem"),
    [
        (_A_DATABASE_ASSERTION, True, False),
        (_A_FILESYSTEM_ASSERTION, False, True),
        # A file assertion in its authored form addresses neither: it is not a
        # JSONPath expression at all.
        ({"path_glob": "/env/fs/agent-visible/x.py", "contains_ci": "ok"}, False, False),
        # Neither of these can be shown to address the filesystem, so both stay in the
        # database-reading population: the state is still fetched and each evaluator
        # names the defect per assertion. Read as addressing nothing, they would be
        # dropped from the fetch and scored against a state never read.
        ({"path": "$.db[[", "equals": "x"}, True, False),
        ({"path": 5, "equals": "x"}, True, False),
    ],
)
def test_an_assertion_is_classified_by_the_state_it_reads(
    assertion: Mapping[str, Any], reads_the_database: bool, reads_the_filesystem: bool
) -> None:
    assert addresses_the_database(assertion) is reads_the_database
    assert addresses_the_filesystem(assertion) is reads_the_filesystem


@pytest.mark.parametrize(
    ("seeds_tables", "declares_schemas", "declares_unstable_fields", "provisions_files"),
    list(product((False, True), repeat=4)),
)
def test_provisions_database_reads_every_field_the_db_service_is_initialised_from(
    seeds_tables: bool,
    declares_schemas: bool,
    declares_unstable_fields: bool,
    provisions_files: bool,
) -> None:
    initial_state = RunnerInitialStateConfig(
        tables=_A_TABLE if seeds_tables else {},
        schemas=_A_SCHEMA if declares_schemas else [],
        unstable_fields=_AN_UNSTABLE_FIELD if declares_unstable_fields else [],
        filesystem=_A_PROVISIONED_FILE if provisions_files else {},
    )
    declares_db_state = seeds_tables or declares_schemas or declares_unstable_fields
    assert provisions_database(initial_state) is declares_db_state


@pytest.mark.parametrize(
    ("state_checks", "expected"),
    [
        ({"jsonpaths": [_A_DATABASE_ASSERTION]}, True),
        ({"jsonpaths": [_A_FILESYSTEM_ASSERTION]}, False),
        # An enabled hash block with no source at all: the shape that compares
        # against UNDECLARED_INITIAL_STATE, which is still read out of the database.
        ({"hash": {"enabled": True}}, True),
        ({"db_probes": [_A_PROBE]}, False),
    ],
)
def test_block_addresses_the_database_over_each_state_source(
    state_checks: Mapping[str, Any], expected: bool
) -> None:
    assert block_addresses_the_database(state_checks) is expected
