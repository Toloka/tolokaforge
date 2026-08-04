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
    score, _ = combine_grade_components(components, cfg)
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
    score, _ = combine_grade_components(components, cfg)
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
# _build_judge_state_diff — the diff-first default's runner-side seam
# ---------------------------------------------------------------------------


def _trial_context(initial_state):
    """A minimal stand-in exposing only what _build_judge_state_diff reads."""
    import types

    task_desc = types.SimpleNamespace(initial_state=initial_state)
    return types.SimpleNamespace(task_description=task_desc)


class _FakeDBClient:
    """Async db_client returning a fixed final state."""

    def __init__(self, data):
        self._data = data

    async def get_state(self, trial_id, tables=None):
        import types

        return types.SimpleNamespace(data=self._data)


async def test_build_judge_state_diff_none_when_no_db_client():
    import types

    from tolokaforge.runner.models import RunnerInitialStateConfig
    from tolokaforge.runner.service import RunnerServiceImpl

    fake_self = types.SimpleNamespace(db_client=None)
    tc = _trial_context(RunnerInitialStateConfig(tables={"orders": [{"id": 1}]}))
    out = await RunnerServiceImpl._build_judge_state_diff(fake_self, "trial", tc)
    assert out is None


async def test_build_judge_state_diff_none_when_no_initial_tables():
    import types

    from tolokaforge.runner.models import RunnerInitialStateConfig
    from tolokaforge.runner.service import RunnerServiceImpl

    fake_self = types.SimpleNamespace(db_client=_FakeDBClient({"orders": []}))
    tc = _trial_context(RunnerInitialStateConfig(tables={}))
    out = await RunnerServiceImpl._build_judge_state_diff(fake_self, "trial", tc)
    assert out is None


async def test_build_judge_state_diff_renders_modified_row():
    import types

    from tolokaforge.runner.models import RunnerInitialStateConfig, TableSchema
    from tolokaforge.runner.service import RunnerServiceImpl

    initial = RunnerInitialStateConfig(
        tables={"orders": [{"id": 1, "status": "open"}]},
        schemas=[
            TableSchema(
                table_name="orders",
                fields={"id": "integer", "status": "string"},
                primary_key="id",
            )
        ],
    )
    fake_self = types.SimpleNamespace(
        db_client=_FakeDBClient({"orders": [{"id": 1, "status": "shipped"}]})
    )
    out = await RunnerServiceImpl._build_judge_state_diff(
        fake_self, "trial", _trial_context(initial)
    )
    assert out is not None
    assert "orders: 1 modified" in out
    assert 'status: "open" → "shipped"' in out
