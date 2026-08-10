"""Which trials ``RegisterTrial`` gives a database, read from what the task declares.

The decision is not ``bool(tables)``: the DB service is initialised from the rows, the
schemas and the unstable-field specs together, so a task declaring any one of the three
gets a database and every grading branch that reads one reaches it. ``filesystem`` is
the sibling that does not count — it provisions files into the agent's working
directory and no database at all.
"""

from itertools import product

import pytest

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
