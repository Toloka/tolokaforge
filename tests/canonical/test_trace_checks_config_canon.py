"""The ``trace_checks`` vocabulary is closed, and one model crosses to the runner.

Four claims, each over two independent sources:

1. the declared constraint vocabulary, the hand-written
   :data:`~tolokaforge.runner.models.TRACE_CONSTRAINT_KINDS`, and the ten kinds this
   module writes out are the same set — so a kind added to the model without being
   admitted to the vocabulary, or the reverse, fails here;
2. every field of the grade's ``TraceChecksSummary`` is a field of the evaluation's
   ``TraceChecksResult`` under the same name and annotation — three call sites copy
   the one onto the other field by field and nothing else ties the two shapes;
3. a config spanning that vocabulary survives the gRPC round trip byte-identically
   *and* semantically, which the trial spec's JSON depends on;
4. the block an author writes reaches the runner as the same object the core engine
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
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    TraceChecksResult,
    TraceChecksSummary,
    TraceConstraintExpr,
)

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


def test_the_summary_is_a_projection_of_the_evaluation_result():
    """Every summary field is a result field of the same name and annotation.

    :class:`TraceChecksSummary` is the part of :class:`TraceChecksResult` that
    survives onto the ``Grade``, and three call sites copy it across field by field
    — the core fold, the runner's proto build, and the shared-stack dict. Nothing
    else ties the two shapes together, so a field retyped on one of them is a copy
    that silently narrows or widens on the way to ``grade.yaml``.
    """
    summary = {name: field.annotation for name, field in TraceChecksSummary.model_fields.items()}
    result = {name: field.annotation for name, field in TraceChecksResult.model_fields.items()}

    assert summary, "the summary declares no fields, so the subset below is vacuous"
    assert summary.items() <= result.items(), (
        f"TraceChecksSummary declares {sorted(summary)} against TraceChecksResult's "
        f"{sorted(result)}; the fields that do not match by name and annotation are "
        f"{sorted(name for name, hint in summary.items() if result.get(name) is not hint)}"
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


def test_a_config_declaring_on_missing_withhold_survives_the_wire():
    """An ``on_missing: withhold`` config crosses the trial-spec JSON byte-identical and equal.

    The runner rehydrates ``TraceChecksConfig`` from the JSON that rides the
    trial spec, so the withhold declaration reaches the runner as the same
    enum value the author wrote. Both a bare ``present`` carrier and a nested
    ``count`` inside an ``all_of`` are asserted, so a future refactor that
    stopped threading the policy through composites would break here.
    """
    config = TraceChecksConfig(
        constraints=[
            {
                "id": "kb_succeeded",
                "description": "search_kb was called successfully",
                "on_missing": "withhold",
                "require": {
                    "present": {
                        "match": {
                            "kind": "tool_call",
                            "tool": {"equals": "search_kb"},
                            "status": {"equals": "success"},
                        }
                    }
                },
            },
            {
                "id": "kb_called_at_most_once",
                "description": "search_kb was called at most once and succeeded",
                "on_missing": "withhold",
                "require": {
                    "all_of": [
                        {
                            "count": {
                                "match": {
                                    "kind": "tool_call",
                                    "tool": {"equals": "search_kb"},
                                    "status": {"equals": "success"},
                                },
                                "min": 1,
                                "max": 1,
                            }
                        }
                    ]
                },
            },
        ]
    )

    delivered = TraceChecksConfig.model_validate_json(config.model_dump_json())

    assert delivered.model_dump_json() == config.model_dump_json()
    assert delivered == config
    assert [item.on_missing.value for item in delivered.constraints] == ["withhold", "withhold"]


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
