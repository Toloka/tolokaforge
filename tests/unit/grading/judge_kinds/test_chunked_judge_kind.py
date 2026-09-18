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
from tolokaforge.core.grading.judge_kinds.chunked import DEFAULT_CHUNK_SIZE, _chunk_boundaries
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
    from tolokaforge.core.grading.judge_kinds._shared import member_failure_reason

    chunk_result = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        criterion_results=(
            CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
    )
    reason = member_failure_reason(chunk_result, ("c0", "c1"))
    assert reason is not None
    assert "missing verdicts" in reason
    assert "c1" in reason


# ===================================================================
# _chunk_boundaries — pure partition function (issue #1603)
# ===================================================================


def _ids(chunks: list[list[Criterion]]) -> list[list[str]]:
    return [[c.id for c in chunk] for chunk in chunks]


@pytest.mark.parametrize(
    ("n", "chunk_size"),
    [(7, 3), (12, 5), (30, 5), (1, 5), (5, 5)],
)
def test_chunk_boundaries_ungrouped_matches_fixed_k_slicing(n: int, chunk_size: int) -> None:
    """No criterion declares chunk_group → output is identical to
    ``criteria[i:i+chunk_size]`` slicing. This is the byte-parity anchor
    the 20-fixture κ-parity gate depends on."""
    rubric = _binary_rubric(n)
    expected = [
        list(rubric.criteria[i : i + chunk_size])
        for i in range(0, len(rubric.criteria), chunk_size)
    ]

    assert _chunk_boundaries(rubric.criteria, chunk_size) == expected


def test_chunk_boundaries_group_cohesion_fits_one_chunk() -> None:
    """Three criteria sharing one group plus two ungrouped fillers all land
    in the same chunk when chunk_size=5."""
    criteria = [
        Criterion(id="g1", description="g1", chunk_group="wifi"),
        Criterion(id="g2", description="g2", chunk_group="wifi"),
        Criterion(id="g3", description="g3", chunk_group="wifi"),
        Criterion(id="f1", description="f1"),
        Criterion(id="f2", description="f2"),
    ]

    assert _ids(_chunk_boundaries(criteria, 5)) == [["g1", "g2", "g3", "f1", "f2"]]


def test_chunk_boundaries_oversize_group_splits_across_consecutive_chunks() -> None:
    """A group whose size exceeds chunk_size flushes into consecutive
    chunk_size-runs on its own; no other group's criterion joins either
    slice (8 criteria sharing one group, chunk_size=5 → lengths [5, 3])."""
    criteria = [Criterion(id=f"x{i}", description=f"x{i}", chunk_group="x") for i in range(8)]
    criteria.append(Criterion(id="other", description="other"))

    chunks = _ids(_chunk_boundaries(criteria, 5))

    assert chunks[:2] == [["x0", "x1", "x2", "x3", "x4"], ["x5", "x6", "x7"]]
    assert "other" in chunks[-1]
    assert not any("other" in chunk for chunk in chunks[:2])


def test_chunk_boundaries_non_contiguous_group_reordered_to_first_anchor() -> None:
    """Non-contiguous same-group criteria are silently pulled together at
    the group's first-occurrence position (plan default: silent reorder,
    no validation)."""
    criteria = [
        Criterion(id="a", description="a"),
        Criterion(id="b", description="b", chunk_group="x"),
        Criterion(id="c", description="c"),
        Criterion(id="d", description="d", chunk_group="x"),
        Criterion(id="e", description="e"),
    ]

    assert _ids(_chunk_boundaries(criteria, 3)) == [["a", "b", "d"], ["c", "e"]]


def test_chunk_boundaries_three_declared_groups_pack_together() -> None:
    """Three distinct chunk_group names, each small enough to share a chunk
    with neighbours, produce chunks where every group's members are
    contiguous and no group is split unless it individually exceeds
    chunk_size (mirrors the issue's literal 3-group ask)."""
    criteria = [
        Criterion(id="wifi_speed", description="w1", chunk_group="wifi"),
        Criterion(id="food_var", description="f1", chunk_group="food"),
        Criterion(id="wifi_reach", description="w2", chunk_group="wifi"),
        Criterion(id="staff_polite", description="s1", chunk_group="staff"),
        Criterion(id="food_hot", description="f2", chunk_group="food"),
        Criterion(id="staff_quick", description="s2", chunk_group="staff"),
        Criterion(id="loose", description="l1"),
    ]

    chunks = _ids(_chunk_boundaries(criteria, 5))

    for chunk in chunks:
        for group in ("wifi", "food", "staff"):
            group_positions = [i for i, cid in enumerate(chunk) if cid.startswith(group)]
            if len(group_positions) >= 2:
                assert group_positions == list(
                    range(group_positions[0], group_positions[0] + len(group_positions))
                ), f"group {group!r} split across chunk {chunk!r}"
    all_ids = [cid for chunk in chunks for cid in chunk]
    assert sorted(all_ids) == sorted(c.id for c in criteria)


def test_chunk_boundaries_end_to_end_matches_chunk_size_shape() -> None:
    """A rubric with declared chunk_group hints run through the full public
    ``evaluate`` path lands the grouped criteria in the same
    ``chunk_boundaries`` tuple end-to-end (locks the wire between
    ``_chunk_boundaries`` and the evaluate loop)."""
    rubric = Rubric(
        criteria=[
            Criterion(id="w1", description="w1", chunk_group="wifi"),
            Criterion(id="w2", description="w2", chunk_group="wifi"),
            Criterion(id="loose1", description="loose1"),
            Criterion(id="loose2", description="loose2"),
            Criterion(id="w3", description="w3", chunk_group="wifi"),
        ]
    )
    grouped_chunks = _chunk_boundaries(rubric.criteria, 3)
    scripts = [[_submit_call(chunk)] for chunk in grouped_chunks]
    provider = _QueuedProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"chunk_size": 3})

    assert result.status is JudgeStatus.COMPLETED
    assert result.chunk_boundaries == (("w1", "w2", "w3"), ("loose1", "loose2"))
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]
