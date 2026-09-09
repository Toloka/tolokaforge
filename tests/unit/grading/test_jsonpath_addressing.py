"""What state a ``state_checks`` assertion addresses.

``path:`` is not a synonym for "DB-targeting". The core engine composes ``agent``,
``user``, ``db`` and ``filesystem``; the runner composes ``db`` and ``tables`` and
nothing else. So a path is classified by what the *runner* carries — the narrower
substrate — and one it cannot reach is one the two substrates score differently.
"""

from collections.abc import Mapping
from typing import Any

import pytest

from tolokaforge.core.grading.jsonpath_addressing import (
    JsonPathTarget,
    addresses_the_database,
    block_addresses_the_database,
    jsonpath_target,
    unreachable_target,
)

pytestmark = pytest.mark.unit

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
        ("$.db.orders[0].status", JsonPathTarget.TRIAL_DATABASE),
        ('$["db"].orders', JsonPathTarget.TRIAL_DATABASE),
        ("$.tables.widgets[0].status", JsonPathTarget.TRIAL_DATABASE),
        ("$.filesystem['/env/fs/agent-visible/x.py']", JsonPathTarget.FILESYSTEM),
        # The two roots only the core engine composes. Classified apart from the
        # database because the runner resolves neither, and apart from the filesystem
        # because their remedy is to root the path at db, not to write a path_glob.
        ("$.agent.customers[0].balance", JsonPathTarget.BEYOND_THE_RUNNERS_STATE),
        ("$.user.device.mode", JsonPathTarget.BEYOND_THE_RUNNERS_STATE),
        ("$..status", JsonPathTarget.TRIAL_DATABASE),
        ("$.*", JsonPathTarget.TRIAL_DATABASE),
        ("$", JsonPathTarget.TRIAL_DATABASE),
        # A field named for the filesystem, under the database root: what a
        # substring reading of the written expression gets wrong, and what a
        # reading of any segment rather than the first gets wrong.
        ("$.db.notes[0].filesystem_path", JsonPathTarget.TRIAL_DATABASE),
        ("$.db.notes[0].filesystem", JsonPathTarget.TRIAL_DATABASE),
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
    ("assertion", "reads_the_database", "beyond_the_runner"),
    [
        (_A_DATABASE_ASSERTION, True, None),
        # ``$.filesystem[…]`` grades on the runner via
        # ``filesystem_view.read_agent_visible_filesystem`` — it addresses
        # runner-graded state (so ``unreachable_target`` returns ``None``) and
        # reads the filesystem rather than the database (so
        # ``addresses_the_database`` returns ``False``).
        (_A_FILESYSTEM_ASSERTION, False, None),
        ({"path": "$.agent.customers[0].balance"}, False, JsonPathTarget.BEYOND_THE_RUNNERS_STATE),
        # A file assertion in its authored form addresses neither: it is not a
        # JSONPath expression at all.
        ({"path_glob": "/env/fs/agent-visible/x.py", "contains_ci": "ok"}, False, None),
        # Neither of these can be shown to address the filesystem, so both stay in the
        # database-reading population: the state is still fetched and each evaluator
        # names the defect per assertion. Read as addressing nothing, they would be
        # dropped from the fetch and scored against a state never read.
        ({"path": "$.db[[", "equals": "x"}, True, None),
        ({"path": 5, "equals": "x"}, True, None),
    ],
)
def test_an_assertion_is_classified_by_the_state_it_reads(
    assertion: Mapping[str, Any],
    reads_the_database: bool,
    beyond_the_runner: JsonPathTarget | None,
) -> None:
    assert addresses_the_database(assertion) is reads_the_database
    assert unreachable_target(assertion) is beyond_the_runner


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
