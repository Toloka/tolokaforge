"""The authored ``state_checks.hash.golden_actions`` shapes no replay can iterate.

Three surfaces have to answer the same authored values — the authoring gate, core's
grade-time read and the native description build — and each is driven over a different
pack, so the tables live here and take the pack's tool name as their one variable. A fifth
shape added to a table therefore reaches every surface rather than whichever copy it was
written into.

The *falsy* tables deliberately stay with their own tests: the gate reads five spellings
there and the description build six, the empty list being locked for the gate by a test of
its own.
"""

from __future__ import annotations

from typing import Any

import pytest


def sources_no_replay_can_iterate(tool: str) -> tuple[Any, ...]:
    """Every truthy ``golden_actions`` that is no list, and the type name each must report.

    An author reaches the mapping by dropping the ``-`` in front of a single action and the
    string by writing *tool* beside the key. The number and the boolean have no authoring
    story and are here because a read must not iterate them either.
    """
    return (
        pytest.param({"name": tool}, "dict", id="one_action_written_as_a_mapping"),
        pytest.param(tool, "str", id="a_tool_name_written_beside_the_key"),
        pytest.param(3, "int", id="a_number"),
        pytest.param(True, "bool", id="the_flag_written_over_the_source"),
    )


def elements_that_are_no_action(tool: str) -> tuple[Any, ...]:
    """Every element an untyped ``golden_actions`` list can hold that is no action at all.

    Each declares a tool to call as little as an action carrying no ``name`` key does, and
    none of them answers the ``.get`` both substrates' reads open with.
    """
    return (
        pytest.param(tool, id="a_bare_tool_name"),
        pytest.param(3, id="a_number"),
        pytest.param(None, id="a_list_entry_carrying_nothing"),
        pytest.param([tool], id="a_nested_list"),
    )
