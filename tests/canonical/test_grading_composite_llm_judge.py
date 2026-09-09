"""``composite.grade_llm_judge`` — end-to-end judge dispatch parity lock.

The composite deconstructs the substrate reads and delegates to a
resolved :class:`JudgeKind` seam; the reference impl
(``single_shot_rubric``, :class:`SingleShotRubricJudgeKind`) constructs
an :class:`LLMJudge` per :meth:`.evaluate` and drives it. The runner
path resolves the kind + renders the ``initial → final`` state diff
and hands both to the composite via ``run_in_executor``. This suite
constructs an :class:`InProcessGradingSubstrate` over a hand-built
``{initial_tables, final_tables, filesystem_root, kb_search, db_reader}``
fixture, drives :func:`composite.grade_llm_judge` with a scripted
``LLMClient`` (so the loop is deterministic), and asserts that the
``JudgeResult`` carries the same status, score, and per-criterion
verdicts the judge would report against the same evidence.

The scripted client is injected via a scripted
:class:`JudgeModelProvider` — the kind builds its own judge client per
:meth:`.evaluate` via ``judge_model_provider.build(judge_model_config)``,
and the scripted provider returns the queued script. The judge
orchestration under test is the composite's call site + Protocol
dispatch to the resolved kind, not the LLM.

This suite exercises the **live** ``load_judge_kind("single_shot_rubric")``
resolution — the entry-point group ``tolokaforge.judge_kinds`` is
registered in ``pyproject.toml``, so a regression on the seam wire
between the composite and the resolved kind fails these tests first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading import composite
from tolokaforge.core.grading.judge_kinds import JudgeKind
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig, ToolCall
from tolokaforge.core.plugin_registry import load_judge_kind
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    TableSchema,
)

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _ScriptedClient:
    """A scripted ``LoopLLMClient``: returns queued ``GenerationResult`` in order.

    Each script entry is either a list of ``(tool_name, arguments)`` tuples
    (emitted as tool calls) or a plain string (assistant text, no tool calls).
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self._i = 0

    def generate(
        self, system, messages, tools, tool_choice="auto", observation=None
    ) -> GenerationResult:
        if self._i >= len(self._script):
            return GenerationResult(text="(exhausted)", tool_calls=[], usage=Usage())
        step = self._script[self._i]
        self._i += 1
        if isinstance(step, str):
            return GenerationResult(text=step, tool_calls=[], usage=Usage())
        tool_calls = [
            ToolCall(id=f"call_{self._i}_{j}", name=name, arguments=args)
            for j, (name, args) in enumerate(step)
        ]
        return GenerationResult(
            text="",
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.001,
        )

    def classify_loop_error(self, exc: Exception):
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


class _ScriptedJudgeModelProvider:
    """Test :class:`JudgeModelProvider` — returns a preloaded scripted client
    as the ``JudgeModel``. Bypasses the shipped ``litellm`` transport so the
    canonical suite drives the judge loop deterministically."""

    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    def build(self, model_config: ModelConfig):
        return self._client


def _judge_kind_and_provider(
    script: list[Any],
) -> tuple[JudgeKind, _ScriptedJudgeModelProvider, _ScriptedClient]:
    """Resolve the shipped ``single_shot_rubric`` kind + a scripted provider
    (fresh scripted client each call so tests do not share loop state)."""
    client = _ScriptedClient(script)
    provider = _ScriptedJudgeModelProvider(client)
    kind = load_judge_kind("single_shot_rubric")()
    return kind, provider, client


def _render_diff(
    substrate: InProcessGradingSubstrate,
    schemas: list[TableSchema],
) -> str | None:
    """Reproduce the runner-side state-diff render for a canonical test.

    Mirrors :meth:`RunnerServiceImpl._grade_llm_judge` — an empty
    ``initial_state`` declines outright, otherwise the schemas' primary keys
    layer under any per-trial ``id_fields`` (empty in these fixtures)."""
    initial_tables = substrate.initial_state()
    if not initial_tables:
        return None
    primary_keys: dict[str, str | list[str]] = {s.table_name: s.primary_key for s in schemas}
    return render_state_diff(
        initial_tables,
        substrate.final_state(),
        primary_keys=primary_keys,
        unstable_fields=set(),
    )


def _rubric() -> Rubric:
    """Two non-required criteria (binary weight 2.0 + graded weight 1.0)."""
    return Rubric(
        criteria=[
            Criterion(
                id="refund_done",
                description="Refund issued",
                kind="binary",
                weight=2.0,
            ),
            Criterion(
                id="tone",
                description="Polite tone",
                kind="graded",
                weight=1.0,
            ),
        ]
    )


def _submit_args(**criteria: Any) -> dict[str, Any]:
    """Build a well-formed ``submit_report`` payload from ``{id: verdict}``."""
    args: dict[str, Any] = {"reasons": "overall summary"}
    for cid, verdict in criteria.items():
        args[cid] = verdict
        if isinstance(verdict, bool):
            marker = "VERDICT: MET" if verdict else "VERDICT: NOT MET"
        else:
            marker = f"SCORE: {verdict}"
        args[f"{cid}_justification"] = f"because {cid}\n{marker}"
    return args


def _substrate(
    *,
    initial_tables: dict[str, Any] | None = None,
    final_tables: dict[str, Any] | None = None,
) -> InProcessGradingSubstrate:
    """Substrate exposing the reads the judge touches: DB reader seam (the
    judge's read-only tools bridge to it), initial + final tables (rendered
    into ``state_diff`` runner-side), no KB and no filesystem in this
    fixture."""
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state=initial_tables or {},
        final_state=final_tables or {},
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-composite-grade-llm-judge")


def _drive_grade_llm_judge(
    *,
    config: LLMJudgeConfig,
    substrate: InProcessGradingSubstrate,
    llm_messages: list[dict[str, Any]],
    state_diff: str | None,
    script: list[Any],
    disable_knowledge_search: bool = False,
    custom_system_prompt: str | None = None,
    include_agent_system_prompt: bool = True,
):
    """Drive :func:`composite.grade_llm_judge` through the live
    ``load_judge_kind("single_shot_rubric")`` path with a scripted
    judge model provider."""
    kind, provider, _client = _judge_kind_and_provider(script)
    return composite.grade_llm_judge(
        trial_id="task:0",
        config=config,
        substrate=substrate,
        judge_kind=kind,
        judge_model_provider=provider,
        disable_knowledge_search=disable_knowledge_search,
        custom_system_prompt=custom_system_prompt,
        include_agent_system_prompt=include_agent_system_prompt,
        kind_config=None,
        llm_messages=llm_messages,
        judge_model_config=_JUDGE_MODEL,
        extra_read_tools=[],
        state_diff=state_diff,
        logger=_logger(),
    )


class TestGradeLlmJudgeVerdicts:
    """Every ``JudgeResult`` the composite produces via
    ``load_judge_kind("single_shot_rubric")`` is the same status, score,
    and per-criterion verdicts the judge would report against the same
    evidence."""

    def test_completed_run_returns_scored_criterion_results(self) -> None:
        config = LLMJudgeConfig(rubric=_rubric())
        substrate = _substrate()

        result = _drive_grade_llm_judge(
            config=config,
            substrate=substrate,
            llm_messages=[
                {"role": "system", "content": "you are a refund agent"},
                {"role": "user", "content": "please refund me"},
                {"role": "assistant", "content": "refund processed"},
            ],
            state_diff=_render_diff(substrate, []),
            script=[[("submit_report", _submit_args(refund_done=True, tone=1.0))]],
        )

        assert result.status is JudgeStatus.COMPLETED
        # (2 * 1.0 + 1 * 1.0) / 3 = 1.0
        assert result.score == pytest.approx(1.0)
        assert {cr.id for cr in result.criterion_results} == {"refund_done", "tone"}
        by_id = {cr.id: cr for cr in result.criterion_results}
        assert by_id["refund_done"].met is True
        assert by_id["tone"].score == pytest.approx(1.0)

    def test_state_diff_is_built_from_substrate_reads(self) -> None:
        config = LLMJudgeConfig(rubric=_rubric())
        initial = {"orders": [{"id": 1, "status": "open"}]}
        final = {"orders": [{"id": 1, "status": "refunded"}]}
        substrate = _substrate(initial_tables=initial, final_tables=final)
        schemas = [
            TableSchema(
                table_name="orders",
                fields={"id": "integer", "status": "string"},
                primary_key="id",
            )
        ]

        result = _drive_grade_llm_judge(
            config=config,
            substrate=substrate,
            llm_messages=[
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "please refund me"},
            ],
            state_diff=_render_diff(substrate, schemas),
            script=[[("submit_report", _submit_args(refund_done=True, tone=1.0))]],
        )
        assert result.status is JudgeStatus.COMPLETED
        assert result.state_diff is not None
        assert "orders: 1 modified" in result.state_diff
        assert 'status: "open" → "refunded"' in result.state_diff

    def test_no_initial_state_yields_no_state_diff(self) -> None:
        config = LLMJudgeConfig(rubric=_rubric())
        substrate = _substrate()

        result = _drive_grade_llm_judge(
            config=config,
            substrate=substrate,
            llm_messages=[
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "hi"},
            ],
            state_diff=_render_diff(substrate, []),
            script=[[("submit_report", _submit_args(refund_done=False, tone=0.4))]],
        )
        assert result.status is JudgeStatus.COMPLETED
        assert result.state_diff is None
        by_id = {cr.id: cr for cr in result.criterion_results}
        assert by_id["refund_done"].met is False
        assert by_id["tone"].score == pytest.approx(0.4)

    def test_judge_malfunction_returns_errored_with_no_score(self) -> None:
        """A judge that never calls ``submit_report`` exhausts its budget and
        returns ERRORED with no numeric score — the fail-loud contract the
        composite preserves from the runner."""
        config = LLMJudgeConfig(rubric=_rubric())
        substrate = _substrate()

        result = _drive_grade_llm_judge(
            config=config,
            substrate=substrate,
            llm_messages=[
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "hi"},
            ],
            state_diff=_render_diff(substrate, []),
            script=["turn one, no tool call"] * 20,
        )
        assert result.status is JudgeStatus.ERRORED
        assert result.score is None
        assert result.criterion_results == ()
