"""Unit tests for :class:`VotedRubricJudgeKind`.

Exercises the K-sample loop, the merge contract (joined ``reasons``,
concatenated ``transcript``, ``failed_required_ids`` re-derived on the
full rubric, construction-flag fields taken from sample 0), the
``kind_config`` schema, the aggregator selection (``median`` /
``majority`` end to end), and the fail-loud contract (any sample
ERRORED or missing a criterion verdict yields a whole-trial ERRORED
result; construction-field divergence across samples raises).

Every case drives a scripted :class:`JudgeModelProvider` that pops one
fresh :class:`ScriptedLLMClient` per sample-client build — so the K
samples are deterministic and each sample sees its own script. The
default wrapped kind (``single_shot_rubric``) builds exactly one client
per ``evaluate`` call, so K samples consume exactly K clients.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import VotedRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.voted import (
    DEFAULT_N_SAMPLES,
    _merge_sample_results,
    _sample_failure_reason,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    """Scripted :class:`JudgeModelProvider` that pops one client per ``build``."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)

    def build(self, model_config: ModelConfig):  # noqa: ARG002 — mirrors Protocol
        if not self._clients:
            raise AssertionError(
                "provider ran out of scripted clients; a sample called build more times "
                "than the test expected."
            )
        return self._clients.pop(0)


def _binary_rubric(n: int, *, prefix: str = "c") -> Rubric:
    """``n`` non-required binary criteria."""
    return Rubric(
        criteria=[
            Criterion(id=f"{prefix}{i}", description=f"criterion {i}", kind="binary", weight=1.0)
            for i in range(n)
        ]
    )


def _submit_call(criteria: list[Criterion], verdicts: dict[str, bool] | None = None) -> list:
    """One ``submit_report`` tool-call turn covering every ``criteria`` id with ``verdicts``."""
    v = verdicts or {}
    args: dict[str, Any] = {"reasons": "overall summary"}
    for c in criteria:
        met = v.get(c.id, True)
        args[c.id] = met
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: {'MET' if met else 'NOT MET'}"
    return [("submit_report", args)]


def _clients(scripts: list[list]) -> list[ScriptedLLMClient]:
    return [ScriptedLLMClient(script) for script in scripts]


def _evaluate(
    rubric: Rubric,
    *,
    provider: _QueuedProvider,
    kind_config: dict[str, Any] | None,
) -> JudgeResult:
    """Drive :meth:`VotedRubricJudgeKind.evaluate` with a minimal input surface."""
    kind = VotedRubricJudgeKind()
    return kind.evaluate(
        rubric=rubric,
        agent_system_prompt="you are an agent",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=None,
        kb_search=None,
        workspace_dir=None,
        extra_read_tools=[],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        judge_model_provider=provider,
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        kind_config=kind_config,
        logger=StructuredLogger(name="test-voted"),
    )


def test_k_identical_samples_merge_to_same_verdict() -> None:
    """K=3 identical scripts, default config → merged result equals each sample's verdict."""
    rubric = _binary_rubric(3)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(3)]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.COMPLETED
    assert result.usage.calls == 3
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]
    assert all(cr.met and cr.score == pytest.approx(1.0) for cr in result.criterion_results)
    assert result.score == pytest.approx(1.0)


def test_default_wrapped_kind_is_single_shot_rubric() -> None:
    """Omitting ``wrapped_kind`` builds exactly one client per sample (single_shot_rubric's shape)."""
    rubric = _binary_rubric(2)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(DEFAULT_N_SAMPLES)]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"n_samples": DEFAULT_N_SAMPLES})

    assert result.status is JudgeStatus.COMPLETED
    assert result.usage.calls == DEFAULT_N_SAMPLES


def test_merges_composition_and_gate_fields() -> None:
    """Locks the cross-sample merge: ``reasons`` joined, ``failed_required_ids`` re-derived,
    ``transcript`` concatenated, construction-flag fields taken from sample 0, and the
    per-criterion justification carries the aggregator audit trail."""
    rubric = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="a1", description="a1", kind="binary", weight=1.0, required=True),
        ]
    )
    scripts = [[_submit_call(rubric.criteria, verdicts={"a1": False})] for _ in range(3)]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.COMPLETED
    assert result.reasons.count("overall summary") == 3
    assert "\n\n" in result.reasons
    assert result.failed_required_ids == ("a1",)
    assert result.gate_failed is True
    assert result.include_agent_system_prompt is True
    assert result.custom_system_prompt is False
    assert result.knowledge_search_disabled is False
    assert result.state_diff is None
    assert len(result.transcript) == 3 * 3  # 3 messages per sample loop turn
    assert result.transcript[0]["role"] == "user"
    assert result.chunk_boundaries == ()
    justification = result.criterion_results[0].justification
    assert "aggregator=geometric_median" in justification
    assert "K=3" in justification
    assert "[sample 0]" in justification
    assert "[sample 2]" in justification


@pytest.mark.parametrize(
    ("kind_config", "expected_error_fragment"),
    [
        (None, None),
        ({"n_samples": 3}, None),
        ({"n_samples": 1}, "K<2 makes voting undefined"),
        ({"n_samples": -1}, "K<2 makes voting undefined"),
        ({"n_samples": 1.5}, "must be an int"),
        ({"n_samples": True}, "must be an int"),
        ({"aggregator": "mean"}, "unknown aggregator"),
        ({"aggregator": "majority", "n_samples": 4}, "odd n_samples"),
        ({"unknown_key": "x"}, "unknown_key"),
    ],
)
def test_kind_config_schema(
    kind_config: dict[str, Any] | None,
    expected_error_fragment: str | None,
) -> None:
    """``kind_config`` is validated at ``evaluate`` entry before any judge call."""
    rubric = _binary_rubric(2)
    if expected_error_fragment is None:
        n_samples = (
            kind_config.get("n_samples", DEFAULT_N_SAMPLES) if kind_config else DEFAULT_N_SAMPLES
        )
        provider = _QueuedProvider(
            _clients([[_submit_call(rubric.criteria)] for _ in range(n_samples)])
        )
        result = _evaluate(rubric, provider=provider, kind_config=kind_config)
        assert result.status is JudgeStatus.COMPLETED
        assert result.usage.calls == n_samples
        return

    provider = _QueuedProvider([])
    with pytest.raises(ValueError, match=expected_error_fragment):
        _evaluate(rubric, provider=provider, kind_config=kind_config)


def test_majority_rejects_non_binary_rubric() -> None:
    rubric = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="clarity", description="clarity", kind="graded", weight=1.0),
        ]
    )
    provider = _QueuedProvider([])
    with pytest.raises(ValueError, match="all-binary rubric"):
        _evaluate(rubric, provider=provider, kind_config={"aggregator": "majority", "n_samples": 3})


def test_unknown_wrapped_kind_raises_unknown_implementation_error() -> None:
    rubric = _binary_rubric(2)
    provider = _QueuedProvider([])
    with pytest.raises(UnknownImplementationError):
        _evaluate(rubric, provider=provider, kind_config={"wrapped_kind": "does_not_exist"})


def test_fail_loud_when_sample_never_completes() -> None:
    """Sample 1 of 3 never calls ``submit_report`` → whole-trial ERRORED, sample 2 never dispatched."""
    rubric = _binary_rubric(3)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(3)]
    scripts[1] = ["sample 1 refuses to call submit_report"] * 25
    provider = _QueuedProvider(_clients(scripts[:2]))  # sample 2 must never be built

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert result.binary_pass is None
    assert result.criterion_results == ()
    assert "sample 1" in result.reasons


def test_fail_loud_on_missing_verdict_in_sample() -> None:
    """A sample COMPLETED but missing one rubric criterion id is a fail-loud shape.

    ``parse_submit_report`` rejects (and retries) a ``submit_report`` missing a
    criterion, so this shape cannot be produced by a scripted client — drive the
    failure helper directly, mirroring ``test_fail_loud_on_missing_verdict_in_chunk``.
    """
    sample_result = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        criterion_results=(
            CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
    )
    reason = _sample_failure_reason(sample_result, ("c0", "c1"))
    assert reason is not None
    assert "missing verdicts" in reason
    assert "c1" in reason


def test_construction_field_mismatch_raises() -> None:
    rubric = _binary_rubric(1, prefix="a")
    good = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        score=1.0,
        binary_pass=True,
        criterion_results=(
            CriterionResult(id="a0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
        custom_system_prompt=False,
    )
    divergent = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        score=1.0,
        binary_pass=True,
        criterion_results=(
            CriterionResult(id="a0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
        custom_system_prompt=True,
    )
    with pytest.raises(RuntimeError, match="custom_system_prompt"):
        _merge_sample_results(rubric=rubric, sample_results=[good, divergent], aggregator="median")


def test_median_aggregator_end_to_end() -> None:
    """3 samples vote [True, True, False] on one binary criterion → median score 1.0."""
    rubric = _binary_rubric(1, prefix="a")
    scripts = [
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
    ]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(
        rubric, provider=provider, kind_config={"aggregator": "median", "n_samples": 3}
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.criterion_results[0].score == pytest.approx(1.0)
    assert result.criterion_results[0].met is True


def test_majority_aggregator_end_to_end() -> None:
    """3 samples vote [True, False, False] on one binary criterion → majority score 0.0."""
    rubric = _binary_rubric(1, prefix="a")
    scripts = [
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
    ]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(
        rubric, provider=provider, kind_config={"aggregator": "majority", "n_samples": 3}
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.criterion_results[0].score == 0.0
    assert result.criterion_results[0].met is False
