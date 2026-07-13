"""Light unit tests for ``auto_integration.probes`` pure helpers.

Only the deterministic ``build_units`` flat-pool expansion is covered; ``collect_nodes``
and ``_run_unit`` shell out to pytest and are not unit-tested here.
"""

from __future__ import annotations

import auto_integration.probes as probes


def test_build_units_expands_node_by_rep():
    units = probes.build_units(["n1", "n2"], 3)
    assert units == [
        (0, "n1", 1),
        (0, "n1", 2),
        (0, "n1", 3),
        (1, "n2", 1),
        (1, "n2", 2),
        (1, "n2", 3),
    ]


def test_build_units_reps_one():
    assert probes.build_units(["a", "b"], 1) == [(0, "a", 1), (1, "b", 1)]


def test_build_units_no_nodes_is_empty():
    assert probes.build_units([], 15) == []
