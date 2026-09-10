"""Unit tests for :class:`ChunkedRubricJudgeKind`.

Exercises the chunking loop, the merge contract (joined ``reasons``,
concatenated ``transcript``, ``failed_required_ids`` re-derived on the
original rubric, construction-flag fields taken from chunk 0), the
``kind_config`` schema, and the fail-loud contract (any chunk ERRORED
or missing a chunk-id verdict yields a whole-trial ERRORED result
with ``chunk_boundaries`` still populated).

Every case drives a scripted :class:`JudgeModelProvider` that pops one
fresh :class:`ScriptedLLMClient` per chunk-client build — so the loop is
deterministic and each chunk sees its own script.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import ChunkedRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.chunked import DEFAULT_CHUNK_SIZE
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
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
                "provider ran out of scripted clients; a chunk called build more times "
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
    """One ``submit_report`` tool-call turn covering ``criteria`` with ``verdicts``."""
    v = verdicts or {}
    args: dict[str, Any] = {"reasons": "overall summary"}
    for c in criteria:
        met = v.get(c.id, True)
        args[c.id] = met
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: {'MET' if met else 'NOT MET'}"
    return [("submit_report", args)]


def _chunk_scripts(
    rubric: Rubric, chunk_size: int, verdicts: dict[str, bool] | None = None
) -> list[list]:
    """One well-formed script per chunk over ``rubric`` at ``chunk_size``."""
    chunks = [
        rubric.criteria[i : i + chunk_size] for i in range(0, len(rubric.criteria), chunk_size)
    ]
    return [[_submit_call(chunk, verdicts)] for chunk in chunks]


def _clients(scripts: list[list]) -> list[ScriptedLLMClient]:
    return [ScriptedLLMClient(script) for script in scripts]


def _evaluate(
    rubric: Rubric,
    *,
    provider: _QueuedProvider,
    kind_config: dict[str, Any] | None,
) -> JudgeResult:
    """Drive :meth:`ChunkedRubricJudgeKind.evaluate` with a minimal input surface."""
    kind = ChunkedRubricJudgeKind()
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
        logger=StructuredLogger(name="test-chunked"),
    )


def test_chunks_rubric_and_merges_results() -> None:
    """K=5 over 30 criteria → 6 chunks; merged verdicts cover every id in order."""
    rubric = _binary_rubric(30)
    scripts = _chunk_scripts(rubric, chunk_size=5)
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"chunk_size": 5})

    assert result.status is JudgeStatus.COMPLETED
    assert len(result.chunk_boundaries) == 6
    assert all(len(chunk) == 5 for chunk in result.chunk_boundaries)
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]
    assert result.score == pytest.approx(1.0)
    assert result.usage.calls == 6
    assert result.usage.tool_calls == 0
    assert result.usage.prompt_tokens == 6 * 10
    assert result.usage.completion_tokens == 6 * 5


def test_partial_last_chunk_shape() -> None:
    """12 criteria at K=5 → chunk sizes (5, 5, 2), boundaries preserved in order."""
    rubric = _binary_rubric(12)
    provider = _QueuedProvider(_clients(_chunk_scripts(rubric, chunk_size=5)))

    result = _evaluate(rubric, provider=provider, kind_config={"chunk_size": 5})

    assert [len(chunk) for chunk in result.chunk_boundaries] == [5, 5, 2]
    assert result.chunk_boundaries == (
        tuple(c.id for c in rubric.criteria[0:5]),
        tuple(c.id for c in rubric.criteria[5:10]),
        tuple(c.id for c in rubric.criteria[10:12]),
    )


def test_merges_composition_fields_per_spec() -> None:
    """Locks the cross-chunk merge: ``reasons`` joined by blank line,
    ``failed_required_ids`` re-derived from the original rubric (so the
    gate flag comes out of the whole-rubric fold, not any single chunk),
    ``transcript`` concatenated in chunk order, construction-flag
    fields (``custom_system_prompt`` etc.) taken from chunk 0, plus the
    RuntimeError guard when those construction fields disagree across
    chunks."""
    rubric = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="a1", description="a1", kind="binary", weight=1.0),
            Criterion(id="a2", description="a2", kind="binary", weight=1.0, required=True),
            Criterion(id="a3", description="a3", kind="binary", weight=1.0),
        ]
    )
    scripts = _chunk_scripts(rubric, chunk_size=2, verdicts={"a2": False})
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"chunk_size": 2})

    assert result.status is JudgeStatus.COMPLETED
    assert result.reasons.count("overall summary") == 2
    assert "\n\n" in result.reasons
    assert result.failed_required_ids == ("a2",)
    assert result.gate_failed is True
    assert result.include_agent_system_prompt is True
    assert result.custom_system_prompt is False
    assert result.knowledge_search_disabled is False
    assert result.state_diff is None
    assert len(result.transcript) == sum(1 for _ in range(2)) * 3  # 3 messages per chunk loop
    assert result.transcript[0]["role"] == "user"

    def _mismatched_merge(
        rubric,
        chunk_results,
        chunk_boundaries,
    ):
        from tolokaforge.core.grading.judge_kinds import chunked as _chunked

        return _chunked._merge_chunk_results(
            rubric=rubric,
            chunk_results=chunk_results,
            chunk_boundaries=chunk_boundaries,
        )

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
            CriterionResult(id="a1", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
        custom_system_prompt=True,
    )
    small = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="a1", description="a1", kind="binary", weight=1.0),
        ]
    )
    with pytest.raises(RuntimeError, match="custom_system_prompt"):
        _mismatched_merge(
            small,
            [good, divergent],
            (("a0",), ("a1",)),
        )


@pytest.mark.parametrize(
    ("kind_config", "expected_error_fragment"),
    [
        (None, None),
        ({"chunk_size": 3}, None),
        ({"chunk_size": 0}, "must be >= 1"),
        ({"chunk_size": -1}, "must be >= 1"),
        ({"unknown_key": "x"}, "unknown_key"),
    ],
)
def test_kind_config_schema(
    kind_config: dict[str, Any] | None,
    expected_error_fragment: str | None,
) -> None:
    """``kind_config`` is validated at ``evaluate`` entry before any judge call."""
    rubric = _binary_rubric(7)
    if expected_error_fragment is None:
        chunk_size = 3 if kind_config == {"chunk_size": 3} else DEFAULT_CHUNK_SIZE
        provider = _QueuedProvider(_clients(_chunk_scripts(rubric, chunk_size=chunk_size)))
        result = _evaluate(rubric, provider=provider, kind_config=kind_config)
        assert result.status is JudgeStatus.COMPLETED
        expected_chunks = (len(rubric.criteria) + chunk_size - 1) // chunk_size
        assert len(result.chunk_boundaries) == expected_chunks
        return

    provider = _QueuedProvider([])
    with pytest.raises(ValueError, match=expected_error_fragment):
        _evaluate(rubric, provider=provider, kind_config=kind_config)


def test_fail_loud_on_partial_chunk() -> None:
    """Second-of-three chunks returns ERRORED → whole-trial ERRORED with boundaries."""
    rubric = _binary_rubric(6)
    scripts = _chunk_scripts(rubric, chunk_size=2)
    scripts[1] = ["chunk 1 refuses to call submit_report"] * 25
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"chunk_size": 2})

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert result.binary_pass is None
    assert result.criterion_results == ()
    assert len(result.chunk_boundaries) == 3
    assert result.chunk_boundaries[1] == ("c2", "c3")
    assert "chunk 1" in result.reasons
    assert "c2" in result.reasons and "c3" in result.reasons


def test_fail_loud_on_missing_verdict_in_chunk() -> None:
    """Chunk COMPLETED but missing one of its criterion ids → whole-trial ERRORED.

    A ``submit_report`` that omits a criterion is rejected by
    :func:`parse_submit_report` and retried, so we cannot construct that shape
    from a scripted client's ``submit_report`` args. Instead we synthesise a
    :class:`JudgeResult` whose ``criterion_results`` misses one id and drive it
    through the chunked kind's failure helper directly — the same fail-loud path
    that guards the missing-verdict shape after a hypothetical judge succeeded
    on a partial rubric.
    """
    from tolokaforge.core.grading.judge_kinds.chunked import _chunk_failure_reason

    chunk_result = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        criterion_results=(
            CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
    )
    reason = _chunk_failure_reason(chunk_result, ("c0", "c1"))
    assert reason is not None
    assert "missing verdicts" in reason
    assert "c1" in reason
