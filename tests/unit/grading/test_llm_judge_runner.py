"""Tests for runner-side grade combination + judge config plumbing.

The legacy single-call ``evaluate_llm_judge`` (and its ``_parse_judge_json`` /
``_format_transcript_for_judge`` helpers) was deleted in Stage 4 of the
rubric-grading plan — the judge now runs runner-side on the shared
``ToolCallingLoop`` (see ``tests/unit/grading/test_judge.py`` for its behaviour).
What survives here are the *combine* helpers (which fold the judge component into
the final score) and the config wire round-trip.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# combine_grade_components / build_grade_reasons — judge folded in
# ---------------------------------------------------------------------------


def test_combine_components_includes_llm_judge_when_score_set():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": 0.6,
    }
    cfg = {
        "combine_method": "weighted",
        "weights": {"llm_judge": 1.0},
        "llm_judge": {"rubric": {"criteria": [{"id": "a", "description": "d"}]}},
    }
    score = combine_grade_components(components, cfg).score
    assert score == pytest.approx(0.6)


def test_combine_excludes_llm_judge_when_score_is_sentinel():
    """An ERRORED judge leaves ``llm_judge_score`` at -1.0 — excluded, not 0.0."""
    components = {
        "hash_score": 0.8,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": -1.0,
    }
    cfg = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0, "llm_judge": 1.0},
        "state_checks": {"hash_enabled": True},
    }
    score = combine_grade_components(components, cfg).score
    # Only state_checks is active; the errored judge does NOT drag the score to 0.
    assert score == pytest.approx(0.8)


def test_build_grade_reasons_includes_judge_text():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": 0.92,
    }
    text = build_grade_reasons(components, judge_reasons="rubric satisfied")
    assert "Judge: score=0.92" in text
    assert "rubric satisfied" in text


# ---------------------------------------------------------------------------
# Config wire round-trip — structured rubric survives serialization
# ---------------------------------------------------------------------------


def test_native_adapter_serializes_llm_judge():
    """A structured rubric round-trips through the runner GradingConfig wire shape.

    The judge *model* no longer lives on this block — it relocated to the run
    config (models.judge) and rides ``TrialSpec.judge_model_config``. This pins
    that the grading payload now carries ONLY the rubric, with no ``model_ref``.
    """
    from tolokaforge.runner.models import LLMJudgeConfig, RunnerGradingConfig

    cfg = RunnerGradingConfig(
        combine_method="weighted",
        weights={"llm_judge": 1.0},
        llm_judge=LLMJudgeConfig(
            rubric={
                "reference": "Correct refund is $328.50.",
                "criteria": [
                    {
                        "id": "refund_amount",
                        "description": "Reply quotes the correct refund amount",
                        "expected": "$328.50",
                        "kind": "binary",
                        "required": True,
                        "weight": 1.0,
                    }
                ],
            },
        ),
    )
    payload = cfg.model_dump()
    assert "model_ref" not in payload["llm_judge"]
    assert payload["llm_judge"]["rubric"]["criteria"][0]["id"] == "refund_amount"

    reconstructed = RunnerGradingConfig.model_validate(payload)
    assert reconstructed.llm_judge is not None
    assert reconstructed.llm_judge.rubric.reference == "Correct refund is $328.50."
    crit = reconstructed.llm_judge.rubric.criteria[0]
    assert crit.id == "refund_amount"
    assert crit.required is True
    assert crit.expected == "$328.50"


def test_judge_model_rides_on_trial_spec():
    """The judge model is now a run-level ModelConfig carried on the TrialSpec —
    symmetric with the agent and user models — and survives the gRPC JSON wire."""
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.core.trial import EnvEndpoints, TrialSpec
    from tolokaforge.runner.models import TaskDescription

    judge_model = ModelConfig(
        provider="openrouter", name="anthropic/claude-sonnet-4.6", temperature=0.0
    )
    spec = TrialSpec(
        trial_id="t:0",
        run_id="r",
        task=TaskDescription(
            task_id="t",
            name="t",
            category="test",
            description="d",
            adapter_type="native",
            system_prompt="sys",
        ),
        agent_model_config=ModelConfig(provider="openrouter", name="agent"),
        judge_model_config=judge_model,
        env_endpoints=EnvEndpoints(db_url="http://db", runner_url="http://runner"),
    )

    rehydrated = TrialSpec.model_validate_json(spec.model_dump_json())
    assert rehydrated.judge_model_config == judge_model
    assert rehydrated.judge_model_config.temperature == 0.0


# ---------------------------------------------------------------------------
# composite.build_judge_state_diff — the diff-first default over the substrate
# ---------------------------------------------------------------------------


def _substrate_for_diff(initial_tables, final_tables):
    """An :class:`InProcessGradingSubstrate` carrying only the reads the
    state-diff helper touches: ``initial_state`` (the pre-run tables) and
    ``final_state`` (RAW post-run rows). Everything else the substrate exposes
    stays unused for this helper."""
    from unittest.mock import MagicMock

    from tolokaforge.core.grading.substrate import InProcessGradingSubstrate

    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state=initial_tables,
        final_state=final_tables,
    )


def _logger():
    from tolokaforge.core.logging import StructuredLogger

    return StructuredLogger(name="test-judge-state-diff")


def test_build_judge_state_diff_none_when_no_initial_tables():
    """An empty ``initial_state`` has no baseline to diff against — the judge
    falls back to its read-only tools and no diff is injected."""
    from tolokaforge.core.grading.composite import build_judge_state_diff

    out = build_judge_state_diff(
        trial_id="trial",
        substrate=_substrate_for_diff({}, {"orders": []}),
        initial_state_schemas=[],
        id_fields={},
        unstable_fields=set(),
        logger=_logger(),
    )
    assert out is None


def test_build_judge_state_diff_renders_modified_row():
    from tolokaforge.core.grading.composite import build_judge_state_diff
    from tolokaforge.runner.models import TableSchema

    out = build_judge_state_diff(
        trial_id="trial",
        substrate=_substrate_for_diff(
            {"orders": [{"id": 1, "status": "open"}]},
            {"orders": [{"id": 1, "status": "shipped"}]},
        ),
        initial_state_schemas=[
            TableSchema(
                table_name="orders",
                fields={"id": "integer", "status": "string"},
                primary_key="id",
            )
        ],
        id_fields={},
        unstable_fields=set(),
        logger=_logger(),
    )
    assert out is not None
    assert "orders: 1 modified" in out
    assert 'status: "open" → "shipped"' in out


def test_build_judge_state_diff_layers_declared_id_fields_over_schema_pk():
    """The trial's ``state_checks.id_fields`` is the diff's key source.

    The schema's single ``account_id`` PK repeats per side, so only the declared
    composite key — layered over the schema entry — can match the edit as a
    modification; dropping the layer (or reversing it) degrades to add/remove.
    """
    from tolokaforge.core.grading.composite import build_judge_state_diff
    from tolokaforge.runner.models import TableSchema

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
    out = build_judge_state_diff(
        trial_id="trial",
        substrate=_substrate_for_diff(initial, final),
        initial_state_schemas=[
            TableSchema(
                table_name="positions",
                fields={"account_id": "string", "symbol": "string", "qty": "integer"},
                primary_key="account_id",
            )
        ],
        id_fields={"positions": ["account_id", "symbol"]},
        unstable_fields=set(),
        logger=_logger(),
    )
    assert out is not None
    assert "positions: 1 modified" in out
    assert 'account_id="A1", symbol="MSFT"' in out
    assert "added" not in out and "removed" not in out


def test_build_judge_state_diff_substrate_unreachable_propagates():
    """A :class:`SubstrateUnreachableError` from the substrate's ``final_state`` read
    is NOT swallowed — the dispatch site can translate it to ``GradingFailedError``.
    """
    from unittest.mock import MagicMock

    import pytest

    from tolokaforge.core.grading.composite import build_judge_state_diff
    from tolokaforge.core.grading.substrate import (
        InProcessGradingSubstrate,
        SubstrateUnreachableError,
    )

    def _explode():
        raise SubstrateUnreachableError("the runner went away")

    substrate = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={"orders": [{"id": 1}]},
        final_state_factory=_explode,
    )
    with pytest.raises(SubstrateUnreachableError):
        build_judge_state_diff(
            trial_id="trial",
            substrate=substrate,
            initial_state_schemas=[],
            id_fields={},
            unstable_fields=set(),
            logger=_logger(),
        )


def test_build_judge_state_diff_generic_final_state_failure_degrades_to_none():
    """A non-substrate failure fetching final state (DB hiccup, unexpected
    shape) degrades to no diff so the judge still runs on its read-only tools;
    the components already computed by the outer grade call are preserved.
    """
    from unittest.mock import MagicMock

    from tolokaforge.core.grading.composite import build_judge_state_diff
    from tolokaforge.core.grading.substrate import InProcessGradingSubstrate

    def _explode():
        raise RuntimeError("db hiccup")

    substrate = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={"orders": [{"id": 1}]},
        final_state_factory=_explode,
    )
    out = build_judge_state_diff(
        trial_id="trial",
        substrate=substrate,
        initial_state_schemas=[],
        id_fields={},
        unstable_fields=set(),
        logger=_logger(),
    )
    assert out is None
