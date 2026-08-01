"""The ``trace_checks`` vocabulary is closed, and one model crosses to the runner.

Three claims, each over two independent sources:

1. the declared constraint vocabulary, the hand-written
   :data:`~tolokaforge.runner.models.TRACE_CONSTRAINT_KINDS`, and the ten kinds this
   module writes out are the same set — so a kind added to the model without being
   admitted to the vocabulary, or the reverse, fails here;
2. a config spanning that vocabulary survives the gRPC round trip byte-identically
   *and* semantically, which the trial spec's JSON depends on;
3. the block an author writes reaches the runner as the same object the core engine
   holds. Unlike ``state_checks`` and ``transcript_rules``, whose runner shapes are
   flattened and re-keyed field by field, this one crosses unchanged — so the
   assertion is equality of the whole model, not of the keys someone remembered to
   translate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.trace_checks_configs import every_kind_block
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import TraceChecksConfig
from tolokaforge.runner.models import TRACE_CONSTRAINT_KINDS, TraceConstraintExpr

pytestmark = pytest.mark.canonical

_PARITY_GLOB = "grading_parity/**/task.yaml"
_ALL_KEYS_TASK = "all_keys"

# The vocabulary, written out here so the lock below compares three sources and not
# one against itself: this list, the module's frozenset, and the fields the
# expression model declares.
_THE_TEN_KINDS = (
    "present",
    "absent",
    "count",
    "before",
    "immediately_before",
    "absent_before",
    "absent_between",
    "all_of",
    "any_of",
    "negate",
)


def test_the_constraint_vocabulary_is_closed_at_ten_members():
    assert len(_THE_TEN_KINDS) == 10
    assert set(_THE_TEN_KINDS) == TRACE_CONSTRAINT_KINDS, (
        "the declared vocabulary and the ten members this module names disagree. A kind "
        "the manifest and the docs do not cover is a kind no fixture pack proves"
    )
    assert set(TraceConstraintExpr.model_fields) == TRACE_CONSTRAINT_KINDS, (
        f"TRACE_CONSTRAINT_KINDS holds {sorted(k.value for k in TRACE_CONSTRAINT_KINDS)} but "
        f"TraceConstraintExpr declares {sorted(TraceConstraintExpr.model_fields)}. The "
        "evaluator dispatches on the frozenset and the loader validates against the "
        "fields, so a kind in one and not the other is either unreachable or unvalidated"
    )


def test_a_config_spanning_the_vocabulary_survives_the_wire():
    """The runner receives this block as JSON inside the trial spec.

    Byte identity alone would pass on a dump that lost every distinction, so the
    semantic half is asserted beside it: same constraint ids, same declared kind per
    constraint, and the delivered model equal to the one that was sent.
    """
    config = TraceChecksConfig(**every_kind_block())

    delivered = TraceChecksConfig.model_validate_json(config.model_dump_json())

    assert delivered.model_dump_json() == config.model_dump_json()
    assert delivered == config
    assert [item.id for item in delivered.constraints] == [item.id for item in config.constraints]
    assert {item.require.declared_kind() for item in delivered.constraints} == (
        TRACE_CONSTRAINT_KINDS
    )


def test_the_block_reaches_the_runner_as_the_object_the_engine_holds(test_data_dir: Path):
    """One model, both substrates, passed through the adapter with no translation.

    Asserted as equality of the whole block rather than field by field: there is no
    per-key translation to check, so a key-level assertion would prove less than the
    contract states and would go stale the moment the vocabulary grows.
    """
    adapter = NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": _PARITY_GLOB})

    core_config = adapter.get_grading_config(_ALL_KEYS_TASK)
    runner_grading = adapter.to_task_description(_ALL_KEYS_TASK).grading

    assert core_config.trace_checks is not None, (
        f"the {_ALL_KEYS_TASK} pack declares a trace_checks block but the core config "
        "carries none, so this test would compare two absences"
    )
    assert runner_grading.trace_checks == core_config.trace_checks
    assert [item.id for item in runner_grading.trace_checks.constraints] == [
        "wrote_the_file_before_closing",
        "closed_the_widget_exactly_once",
        "never_deleted_a_widget_unannounced",
        "closed_straight_after_writing_and_told_nobody_meanwhile",
    ]
