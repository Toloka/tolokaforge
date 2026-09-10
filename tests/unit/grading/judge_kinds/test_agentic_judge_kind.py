"""Unit tests for :class:`AgenticRubricJudgeKind` and :class:`_DraftReportTermination`.

Exercises the ``kind_config`` schema, the draft -> critique -> submit state
machine's transitions (valid/invalid draft, repeat draft, premature submit,
valid/invalid post-critique submit and its retry exhaustion, pure-text
turns), the injected critique message's content, the cross-``.run()``-
resumption turn-budget backstop, the ``JudgeUsage`` cost-measurement
plumbing, and the capability-threading contract into the judge's own
``LoopConfig``.

Every case drives a single scripted :class:`ScriptedLLMClient` (the kind
builds its judge model exactly once per episode, unlike ``chunked_rubric``
which builds one per chunk) so the whole draft/critique/submit episode is
deterministic end to end.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge import DEFAULT_SUBMIT_REPORT_RETRIES
from tolokaforge.core.grading.judge_kinds import agentic
from tolokaforge.core.grading.judge_kinds.agentic import (
    AGENTIC_JUDGE_MAX_TURNS,
    DEFAULT_CRITIQUE_TURN_BUDGET,
    AgenticRubricJudgeKind,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.judge_tools import DelegatingReadTool
from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.summarize_policy import LLMSummarizer
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _FakeKnowledgeSearch:
    """A real ``KnowledgeSearch``-conforming stub — no mock, a genuine object."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    def search(
        self, query: str, top_k: int = 5, alpha: float = 0.5
    ) -> list[SearchHit]:  # noqa: ARG002
        return self._hits[:top_k]


class _SingleClientProvider:
    """Scripted ``JudgeModelProvider`` returning the same client every ``build``.

    ``AgenticRubricJudgeKind.evaluate`` builds its judge model exactly once
    per episode (the client is reused across every ``.run()`` resumption), so
    a second ``build()`` call would signal a design regression.
    """

    def __init__(self, client: ScriptedLLMClient) -> None:
        self._client = client
        self.build_calls = 0

    def build(self, model_config: ModelConfig) -> ScriptedLLMClient:  # noqa: ARG002
        self.build_calls += 1
        if self.build_calls > 1:
            raise AssertionError("judge_model_provider.build() called more than once")
        return self._client


def _binary_rubric(n: int, *, prefix: str = "c") -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id=f"{prefix}{i}", description=f"criterion {i}", kind="binary", weight=1.0)
            for i in range(n)
        ]
    )


def _report_call(
    tool_name: str,
    criteria: list[Criterion],
    *,
    verdicts: dict[str, bool] | None = None,
    invalid_ids: set[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """One ``(tool_name, args)`` turn covering ``criteria``.

    A criterion id in ``invalid_ids`` gets a justification with no trailing
    ``VERDICT:`` marker, so :func:`parse_submit_report` raises
    :class:`VerdictConsistencyError` for it.
    """
    v = verdicts or {}
    invalid = invalid_ids or set()
    args: dict[str, Any] = {"reasons": "overall summary"}
    for c in criteria:
        met = v.get(c.id, True)
        args[c.id] = met
        if c.id in invalid:
            args[f"{c.id}_justification"] = "no marker here"
        else:
            args[f"{c.id}_justification"] = (
                f"because {c.id}\nVERDICT: {'MET' if met else 'NOT MET'}"
            )
    return [(tool_name, args)]


def _evaluate(
    rubric: Rubric,
    *,
    provider: _SingleClientProvider,
    kind_config: dict[str, Any] | None = None,
    extra_read_tools: list[Any] | None = None,
    kb_search: Any | None = None,
) -> JudgeResult:
    """Drive :meth:`AgenticRubricJudgeKind.evaluate` with a minimal input surface."""
    kind = AgenticRubricJudgeKind()
    return kind.evaluate(
        rubric=rubric,
        agent_system_prompt="you are an agent",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=None,
        kb_search=kb_search,
        workspace_dir=None,
        extra_read_tools=extra_read_tools or [],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        judge_model_provider=provider,
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        kind_config=kind_config,
        logger=StructuredLogger(name="test-agentic"),
    )


def _tool_message_contents(result: JudgeResult) -> list[str]:
    return [str(m["content"]) for m in result.transcript if m.get("role") == "tool"]


def _user_message_contents(result: JudgeResult) -> list[str]:
    return [str(m["content"]) for m in result.transcript if m.get("role") == "user"]


def _critique_call(
    criteria: list[Criterion], *, verdicts: dict[str, bool] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """One ``critique(verdict_draft=...)`` turn wrapping a ``submit_report``-shaped draft."""
    ((_, args),) = _report_call("submit_report", criteria, verdicts=verdicts)
    return [("critique", {"verdict_draft": args})]


@pytest.mark.parametrize(
    ("kind_config", "expected_error_fragment"),
    [
        (None, None),
        ({"critique_turn_budget": 5}, None),
        ({"critique_turn_budget": 0}, "must be >= 1"),
        ({"unknown_key": "x"}, "unknown_key"),
        ({"enable_critique_tool": False}, None),
        ({"enable_critique_tool": "yes"}, "enable_critique_tool"),
    ],
)
def test_kind_config_schema(
    kind_config: dict[str, Any] | None,
    expected_error_fragment: str | None,
) -> None:
    """``kind_config`` is validated at ``evaluate`` entry before any judge dispatch."""
    rubric = _binary_rubric(2)
    if expected_error_fragment is None:
        script = [
            _report_call("draft_report", rubric.criteria),
            _report_call("submit_report", rubric.criteria),
        ]
        provider = _SingleClientProvider(ScriptedLLMClient(script))
        result = _evaluate(rubric, provider=provider, kind_config=kind_config)
        assert result.status is JudgeStatus.COMPLETED
        return

    provider = _SingleClientProvider(ScriptedLLMClient([]))
    with pytest.raises(ValueError, match=expected_error_fragment):
        _evaluate(rubric, provider=provider, kind_config=kind_config)
    build_before_validation_msg = (
        "kind_config must be validated before judge_model_provider.build()"
    )
    assert provider.build_calls == 0, build_before_validation_msg


def test_agentic_kind_config_accepts_enable_critique_tool_flag() -> None:
    """``enable_critique_tool: False`` is accepted and the episode still completes."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider, kind_config={"enable_critique_tool": False})

    assert result.status is JudgeStatus.COMPLETED


def test_agentic_kind_config_rejects_non_bool_enable_critique_tool() -> None:
    """A non-``bool`` ``enable_critique_tool`` is rejected before any judge dispatch."""
    rubric = _binary_rubric(1)
    provider = _SingleClientProvider(ScriptedLLMClient([]))

    with pytest.raises(ValueError, match="enable_critique_tool"):
        _evaluate(rubric, provider=provider, kind_config={"enable_critique_tool": "yes"})
    assert provider.build_calls == 0


def test_critique_tool_call_between_draft_and_submit_resumes_loop() -> None:
    """A scripted critique(verdict_draft=...) call between draft and submit resumes the loop.

    ``critique`` is a plain registered tool, not a termination trigger, so it
    must be executed and answered like any other tool call — never pausing
    the episode the way ``draft_report``/``submit_report`` do.
    """
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        _critique_call(rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    tool_messages = _tool_message_contents(result)
    assert "{}" in tool_messages, "critique found no evidence in the minimal test transcript"


def _tool_result_for_call(result: JudgeResult, tool_name: str) -> str:
    """Content of the ``role=tool`` message answering the first call to ``tool_name``."""
    call_id = next(
        tc["id"]
        for msg in result.transcript
        for tc in msg.get("tool_calls") or []
        if tc["name"] == tool_name
    )
    return next(
        str(msg["content"]) for msg in result.transcript if msg.get("tool_call_id") == call_id
    )


def test_critique_replays_live_search_kb_hit_through_full_episode_path() -> None:
    """A real ``search_kb`` hit, run earlier in the SAME episode, survives to ``critique``.

    Drives ``draft_report -> search_kb -> critique -> submit_report`` through the
    real ``_build_episode_setup`` -> ``ToolCallingLoop.run`` -> tool-execute path
    with a genuine ``KnowledgeSearch`` stub — not a hand-built static ``messages``
    list — proving the ``CritiqueTool`` sees the ``search_kb`` call/result the
    loop appends in place to the very list it was constructed with.
    """
    rubric = _binary_rubric(1)
    hit = SearchHit(
        doc_id="criterion-0-policy",
        source="kb",
        score=0.9,
        text="Policy note covering criterion 0 in detail.",
    )
    kb_search = _FakeKnowledgeSearch([hit])
    script = [
        _report_call("draft_report", rubric.criteria),
        [("search_kb", {"query": "criterion 0"})],
        _critique_call(rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider, kb_search=kb_search)

    assert result.status is JudgeStatus.COMPLETED
    critique_output = json.loads(_tool_result_for_call(result, "critique"))
    pointers = critique_output["c0"]
    assert any(
        p["source"] == "kb" and p["kb_doc_id"] == "criterion-0-policy" for p in pointers
    ), f"expected a kb-source pointer for c0, got {pointers}"


def test_draft_then_submit_flow_completes() -> None:
    """draft_report then submit_report -> COMPLETED with the submitted verdicts."""
    rubric = _binary_rubric(2)
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]
    assert provider.build_calls == 1
    assert result.usage.calls >= 2


def test_usage_counts_llm_calls_and_an_executed_read_tool_call() -> None:
    """``JudgeResult.usage`` tallies every LLM call and every tool that actually ran.

    ``draft_report``/``submit_report`` calls terminate their turn before
    ``ToolCallingLoop`` executes any tool, so they never increment
    ``usage.tool_calls`` themselves — a real read tool exercised during the
    critique phase is what proves the tool-call side of the counter works.
    """
    rubric = _binary_rubric(1)
    peek_calls: list[dict[str, Any]] = []
    peek_tool = DelegatingReadTool(
        name="peek",
        description="Peek at something",
        parameters={"type": "object", "properties": {}},
        invoke=lambda args: (peek_calls.append(args) or "peeked"),
    )
    script = [
        _report_call("draft_report", rubric.criteria),
        [("peek", {})],
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider, extra_read_tools=[peek_tool])

    assert result.status is JudgeStatus.COMPLETED
    assert peek_calls == [{}]
    assert result.usage.calls >= 3
    assert result.usage.tool_calls >= 1


def test_injected_critique_message_echoes_draft_verdicts_and_budget() -> None:
    """The injected critique prompt echoes the draft's verdicts and the turn budget."""
    rubric = _binary_rubric(2)
    script = [
        _report_call("draft_report", rubric.criteria, verdicts={"c0": True, "c1": False}),
        _report_call("submit_report", rubric.criteria, verdicts={"c0": True, "c1": False}),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider, kind_config={"critique_turn_budget": 7})

    assert result.status is JudgeStatus.COMPLETED
    critique_messages = [m for m in _user_message_contents(result) if "draft verdict was" in m]
    assert len(critique_messages) == 1
    critique = critique_messages[0]
    assert "[c0] MET" in critique
    assert "[c1] NOT MET" in critique
    assert "7 turn(s)" in critique


def test_draft_while_critiquing_injects_corrective_and_continues() -> None:
    """A repeat draft_report call after critiquing has begun is rejected, not re-captured."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    tool_messages = _tool_message_contents(result)
    assert any("draft_report already submitted" in m for m in tool_messages)


def test_submit_while_awaiting_draft_injects_corrective_and_continues() -> None:
    """A premature submit_report (before any draft) is rejected, then the episode proceeds."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("submit_report", rubric.criteria),
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    tool_messages = _tool_message_contents(result)
    assert any("call draft_report first" in m for m in tool_messages)


def test_invalid_draft_args_triggers_retry_via_answer_terminating_submit_report() -> None:
    """A malformed draft_report is rejected via a tool-result rewrite, then retried."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria, invalid_ids={"c0"}),
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    tool_messages = _tool_message_contents(result)
    assert any("draft_report was rejected" in m for m in tool_messages)


def test_invalid_submit_args_triggers_retry_via_answer_terminating_submit_report() -> None:
    """A malformed post-critique submit_report is rejected, then retried, to COMPLETED."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria, invalid_ids={"c0"}),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    tool_messages = _tool_message_contents(result)
    assert any("submit_report was rejected" in m for m in tool_messages)


def test_invalid_submit_args_exhausts_retries_to_errored() -> None:
    """Post-critique submit_report invalid on every attempt exhausts retries -> ERRORED."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        *(
            _report_call("submit_report", rubric.criteria, invalid_ids={"c0"})
            for _ in range(DEFAULT_SUBMIT_REPORT_RETRIES + 1)
        ),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert f"submit_report invalid after {DEFAULT_SUBMIT_REPORT_RETRIES} retries" in result.reasons


def test_no_tool_call_pure_text_turn_silently_continues() -> None:
    """A pure-text turn between tool calls advances the loop without injecting anything."""
    rubric = _binary_rubric(1)
    script = [
        "let me think about this rubric",
        _report_call("draft_report", rubric.criteria),
        "still thinking before I finalize",
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)


def test_max_turns_hard_backstop_at_50() -> None:
    """50 repeated draft_report calls exhaust AGENTIC_JUDGE_MAX_TURNS across resumptions."""
    rubric = _binary_rubric(1)
    script = [_report_call("draft_report", rubric.criteria)] * AGENTIC_JUDGE_MAX_TURNS
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert f"{AGENTIC_JUDGE_MAX_TURNS}-turn budget" in result.reasons


def test_capabilities_threaded_into_loop_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge model's capabilities are threaded into every LoopConfig built."""
    rubric = _binary_rubric(1)
    caps = ModelCapabilities(
        empty_retry_count=2,
        output_length_retry_count=3,
        parser_error_retry_count=4,
        tool_output_max_chars=1000,
        max_context_tokens=8000,
        context_watermark=6000,
    )
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    client = ScriptedLLMClient(script, capabilities=caps)
    provider = _SingleClientProvider(client)

    captured: list[Any] = []
    real_build_loop = agentic._build_loop

    def _spy(*args: Any, **kwargs: Any):
        loop = real_build_loop(*args, **kwargs)
        captured.append(loop.config)
        return loop

    monkeypatch.setattr(agentic, "_build_loop", _spy)

    result = _evaluate(rubric, provider=provider)

    assert result.status is JudgeStatus.COMPLETED
    assert captured, "expected at least one LoopConfig to have been built"
    for config in captured:
        assert config.empty_retry_count == 2
        assert config.output_length_retry_count == 3
        assert config.parser_error_retry_count == 4
        assert config.tool_output_max_chars == 1000
        assert config.max_context_tokens == 8000
        assert config.context_watermark == 6000
        assert isinstance(config.summarize_policy, LLMSummarizer)


def test_default_critique_turn_budget_used_when_kind_config_omits_it() -> None:
    """Locks the default critique-turn-budget constant appearing in the injected message."""
    rubric = _binary_rubric(1)
    script = [
        _report_call("draft_report", rubric.criteria),
        _report_call("submit_report", rubric.criteria),
    ]
    provider = _SingleClientProvider(ScriptedLLMClient(script))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    critique = next(m for m in _user_message_contents(result) if "draft verdict was" in m)
    assert f"{DEFAULT_CRITIQUE_TURN_BUDGET} turn(s)" in critique
