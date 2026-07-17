"""Deterministic orchestration tests for the read-only rubric judge (Stage 4).

These drive the REAL ``run_rubric_judge`` / ``ToolCallingLoop`` / ``rubric.py``
with a SCRIPTED ``LoopLLMClient`` — a fake that returns pre-set tool calls. That
is the right level: it tests OUR orchestration (termination on submit_report,
bounded re-prompt, gating, usage accounting, fail-loud), not the LLM. The DB
reader is a tiny in-memory fake exercising the read-only tool path.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge import JudgeStatus, run_rubric_judge
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import ModelConfig, ToolCall
from tolokaforge.runner.models import Rubric

pytestmark = pytest.mark.unit

# The judge now takes a run-level ModelConfig. These tests inject a scripted
# ``llm_client`` so the config is never used to build a real client; it is
# supplied only to satisfy the required signature.
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedClient:
    """A scripted ``LoopLLMClient``: returns queued GenerationResults in order.

    Each entry is either a list of ``(tool_name, arguments)`` tuples (emitted as
    tool calls) or a plain string (assistant text, no tool calls). Records the
    tools/messages it was driven with for assertions.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.seen_tool_names: list[str] = []

    def generate(
        self, system, messages, tools, tool_choice="auto", observation=None
    ) -> GenerationResult:
        self.calls += 1
        self.seen_tool_names = [t["function"]["name"] for t in tools]
        if self._i >= len(self._script):
            return GenerationResult(text="(no more script)", tool_calls=[], usage=Usage())
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


class FakeDBReader:
    def __init__(self, state: dict | None = None):
        self.state = state or {"orders": [{"id": "o1", "status": "refunded"}]}
        self.get_state_calls = 0

    def get_state(self, tables=None):
        self.get_state_calls += 1
        return self.state

    def query(self, jsonpath):
        return {"results": [self.state]}


# ---------------------------------------------------------------------------
# Rubric fixtures
# ---------------------------------------------------------------------------


def _binary_rubric() -> Rubric:
    return Rubric(
        criteria=[
            {"id": "refund_done", "description": "Refund issued", "kind": "binary", "weight": 1.0},
        ],
        reference="The refund must be issued.",
    )


def _two_criteria_rubric(required: bool = False) -> Rubric:
    return Rubric(
        criteria=[
            {
                "id": "refund_done",
                "description": "Refund issued",
                "kind": "binary",
                "required": required,
                "weight": 2.0,
            },
            {"id": "tone", "description": "Polite tone", "kind": "graded", "weight": 1.0},
        ]
    )


def _verdict_marker(verdict) -> str:
    """The trailing marker line a well-formed justification must carry.

    A ``bool`` verdict is a binary criterion (VERDICT: MET / NOT MET); a real
    number is graded (SCORE: <value>). Any other type is a deliberately malformed
    verdict whose type check fires before the marker check, so no marker matters.
    """
    if isinstance(verdict, bool):
        return "VERDICT: MET" if verdict else "VERDICT: NOT MET"
    if isinstance(verdict, (int, float)):
        return f"SCORE: {verdict}"
    return ""


def _submit_args(**criteria) -> dict:
    """Build a well-formed submit_report payload from {id: verdict} kwargs."""
    args: dict = {"reasons": "overall summary"}
    for cid, verdict in criteria.items():
        args[cid] = verdict
        args[f"{cid}_justification"] = f"because {cid}\n{_verdict_marker(verdict)}"
    return args


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_terminates_on_submit_report_and_scores():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="You are a refund agent.",
        transcript=[{"role": "user", "content": "refund me"}],
        db_reader=FakeDBReader(),
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)
    assert len(result.criterion_results) == 1
    assert result.criterion_results[0].id == "refund_done"
    assert result.criterion_results[0].met is True
    assert client.calls == 1
    # Stage 5: the judge captures its own transcript for the audit bundle, incl.
    # the submit_report tool call.
    assert result.transcript
    roles = [m["role"] for m in result.transcript]
    assert "user" in roles and "assistant" in roles
    names = [tc["name"] for m in result.transcript for tc in m.get("tool_calls", [])]
    assert "submit_report" in names


def test_inspects_db_then_submits():
    rubric = _binary_rubric()
    reader = FakeDBReader()
    client = ScriptedClient(
        [
            [("get_db_state", {})],
            [("submit_report", _submit_args(refund_done=True))],
        ]
    )

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="policy",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=reader,
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    assert reader.get_state_calls == 1
    assert "get_db_state" in client.seen_tool_names
    assert "submit_report" in client.seen_tool_names


class FakeKnowledgeSearch:
    """A stub ``KnowledgeSearch`` returning fixed hits; records its queries.

    Exercises the contract the judge depends on without an HTTP / RAG service —
    asserts the judge's ``search_kb`` surfaces exactly the backend's hits (the
    core regression: judge searches the same index the agent did).
    """

    def __init__(self, hits=None):
        from tolokaforge.core.grading.kb_search import SearchHit

        self._hits = (
            hits
            if hits is not None
            else [
                SearchHit(
                    doc_id="policy_42",
                    source="refund_policy.md",
                    score=0.97,
                    text="Refunds within 30 days.",
                ),
            ]
        )
        self.queries: list[tuple[str, int, float]] = []

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5):
        self.queries.append((query, top_k, alpha))
        return list(self._hits)


def test_search_kb_offered_only_when_kb_resolved():
    """Faithful gating: search_kb iff a KnowledgeSearch is resolved; absent otherwise."""
    rubric = _binary_rubric()

    with_kb = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=FakeKnowledgeSearch(),
        llm_client=with_kb,
    )
    assert "search_kb" in with_kb.seen_tool_names

    without_kb = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=None,
        llm_client=without_kb,
    )
    assert "search_kb" not in without_kb.seen_tool_names


def test_search_kb_surfaces_backend_hits():
    """The judge's search_kb routes to the resolved backend and surfaces its hits."""
    rubric = _binary_rubric()
    kb = FakeKnowledgeSearch()
    client = ScriptedClient(
        [
            [("search_kb", {"query": "refund window", "top_k": 3})],
            [("submit_report", _submit_args(refund_done=True))],
        ]
    )

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=kb,
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    # The judge delegated to the backend with the model's query/top_k.
    assert kb.queries == [("refund window", 3, 0.5)]
    # The backend's hit surfaced in the judge's tool-result transcript.
    tool_results = [m["content"] for m in result.transcript if m.get("tool_call_id")]
    assert any("policy_42" in (c or "") for c in tool_results)
    assert any("refund_policy.md" in (c or "") for c in tool_results)


# ---------------------------------------------------------------------------
# Passthrough read tool (extra_read_tools) — the search_policy reuse path
# ---------------------------------------------------------------------------


def _search_policy_schema() -> dict:
    """A realistic foreign-tool schema (NOT the harness search_kb schema)."""
    return {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Policy question to search"},
            "domain": {"type": "string", "description": "Policy domain"},
        },
        "required": ["question"],
    }


def test_extra_read_tool_offered_with_its_own_schema():
    """A passthrough tool is offered to the judge with the foreign tool's schema."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    rubric = _binary_rubric()
    calls: list[dict] = []

    tool = DelegatingReadTool(
        name="search_policy",
        description="Search the policy KB",
        parameters=_search_policy_schema(),
        invoke=lambda args: (calls.append(args) or "policy result text"),
    )

    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        extra_read_tools=[tool],
        llm_client=client,
    )

    assert "search_policy" in client.seen_tool_names
    # Crucially the LLM sees the FOREIGN tool's real schema, not search_kb's.
    assert tool.get_schema() == {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Search the policy KB",
            "parameters": _search_policy_schema(),
        },
    }


def test_extra_read_tool_absent_when_not_supplied():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        extra_read_tools=None,
        llm_client=client,
    )
    assert "search_policy" not in client.seen_tool_names


def test_extra_read_tool_delegates_and_surfaces_output_verbatim():
    """The judge's passthrough forwards args and relays the foreign output verbatim."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    rubric = _binary_rubric()
    received: list[dict] = []
    tool = DelegatingReadTool(
        name="search_policy",
        description="Search the policy KB",
        parameters=_search_policy_schema(),
        invoke=lambda args: (received.append(args) or "VERBATIM_POLICY_PAYLOAD"),
    )
    client = ScriptedClient(
        [
            [("search_policy", {"question": "refund window", "domain": "refunds"})],
            [("submit_report", _submit_args(refund_done=True))],
        ]
    )

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        extra_read_tools=[tool],
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    # Args passed through unchanged.
    assert received == [{"question": "refund window", "domain": "refunds"}]
    # Foreign output surfaced verbatim in the judge transcript.
    tool_results = [m["content"] for m in result.transcript if m.get("tool_call_id")]
    assert any("VERBATIM_POLICY_PAYLOAD" in (c or "") for c in tool_results)


def test_extra_read_tool_fails_loud_when_foreign_tool_raises():
    """A raising foreign tool → ToolResult(success=False), never swallowed."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    def boom(_args: dict) -> str:
        raise RuntimeError("typesense down")

    tool = DelegatingReadTool(
        name="search_policy",
        description="Search the policy KB",
        parameters=_search_policy_schema(),
        invoke=boom,
    )

    res = tool.execute(question="anything")
    assert res.success is False
    assert "typesense down" in (res.error or "")
    assert res.output == ""


def test_extra_read_tool_bridges_a_sync_call_into_an_async_invoke():
    """The sync judge tool can drive an ASYNC underlying call via a bridging invoke.

    Mirrors how the runner wires ``invoke=lambda args: self._run_async(
    agent_tool.execute(args))``: a synchronous ``execute`` that ends up running a
    coroutine on a separate event loop thread. Here we drive a real async tool
    through a real (separate-thread) event loop to genuinely exercise the bridge.
    """
    import asyncio
    import threading

    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    async def async_search(arguments: dict) -> str:
        await asyncio.sleep(0)  # force a real await on the loop
        return f"async-hit:{arguments['question']}"

    def bridged_invoke(args: dict) -> str:
        fut = asyncio.run_coroutine_threadsafe(async_search(args), loop)
        return fut.result(timeout=5.0)

    try:
        tool = DelegatingReadTool(
            name="search_policy",
            description="Search the policy KB",
            parameters=_search_policy_schema(),
            invoke=bridged_invoke,
        )
        res = tool.execute(question="refunds")
        assert res.success is True
        assert res.output == "async-hit:refunds"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Observability: kb_tools_offered + the "Judge KB: …" reasons note (issue #95)
# ---------------------------------------------------------------------------


def test_kb_observability_rag_search_kb_offered():
    """rag-service KB resolved → kb_tools_offered=('search_kb',) + reasons note."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=FakeKnowledgeSearch(),
        llm_client=client,
    )
    assert result.kb_tools_offered == ("search_kb",)
    assert "Judge KB: search_kb" in result.reasons


def test_kb_observability_search_policy_offered():
    """TypeSense passthrough (extra_read_tools) → kb_tools_offered=('search_policy',)."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    rubric = _binary_rubric()
    tool = DelegatingReadTool(
        name="search_policy",
        description="Search the policy KB",
        parameters=_search_policy_schema(),
        invoke=lambda args: "policy result text",
    )
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        extra_read_tools=[tool],
        llm_client=client,
    )
    assert result.kb_tools_offered == ("search_policy",)
    assert "Judge KB: search_policy" in result.reasons


def test_kb_observability_none_offered_is_recorded_not_errored():
    """No KB backend → kb_tools_offered=() + 'Judge KB: none offered' — NOT an error."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=None,
        extra_read_tools=None,
        llm_client=client,
    )
    # Observability, not failure: a KB-less judge still COMPLETES.
    assert result.status is JudgeStatus.COMPLETED
    assert result.kb_tools_offered == ()
    assert "Judge KB: none offered" in result.reasons


def test_kb_note_surfaced_even_when_judge_errors():
    """An ERRORED judge still records which KB it had — debugging signal (#95)."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("get_db_state", {})]] * 50)  # never submits → turn exhaustion
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=None,
        max_turns=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.ERRORED
    assert result.kb_tools_offered == ()
    assert "Judge KB: none offered" in result.reasons


def test_db_tools_absent_when_no_reader():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=None,
        llm_client=client,
    )
    assert "get_db_state" not in client.seen_tool_names
    assert "query_db" not in client.seen_tool_names
    assert "submit_report" in client.seen_tool_names


def test_malformed_submit_report_reprompts_then_errors():
    """Invalid submit_report → bounded re-prompt → ERRORED with NO score."""
    rubric = _binary_rubric()
    bad = _submit_args(refund_done="yes")  # binary expects a bool
    client = ScriptedClient(
        [
            [("submit_report", bad)],
            [("submit_report", bad)],
            [("submit_report", bad)],
        ]
    )

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert result.binary_pass is None
    assert client.calls == 3  # initial + 2 retries
    # A schema (type) rejection is NOT a verdict-consistency rejection: the
    # generic retry still fired, but the consistency counter stays 0.
    assert result.usage.consistency_rejections == 0


def _contradicting_submit(refund_done: bool) -> dict:
    """A submit_report whose marker line contradicts the submitted verdict."""
    opposite = "VERDICT: NOT MET" if refund_done else "VERDICT: MET"
    return {
        "refund_done": refund_done,
        "refund_done_justification": f"reasoning about the refund\n{opposite}",
        "reasons": "overall summary",
    }


def _missing_marker_submit(refund_done: bool) -> dict:
    """A well-typed submit_report whose justification has no trailing marker."""
    return {
        "refund_done": refund_done,
        "refund_done_justification": "reasoning with no verdict marker line",
        "reasons": "overall summary",
    }


def test_clean_run_reports_zero_consistency_rejections():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    assert result.usage.consistency_rejections == 0


def test_consistency_rejection_reprompts_and_counts_per_attempt_then_errors():
    """Contradicting marker → re-prompt; counter increments per rejected attempt;
    after retry exhaustion the result is ERRORED with the count reflecting attempts."""
    rubric = _binary_rubric()
    bad = _contradicting_submit(refund_done=True)  # verdict MET, marker NOT MET
    client = ScriptedClient([[("submit_report", bad)]] * 3)
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert client.calls == 3  # initial + 2 retries, each rejected
    assert result.usage.consistency_rejections == 3


def test_missing_marker_counts_as_consistency_rejection():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _missing_marker_submit(refund_done=True))]] * 3)
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.ERRORED
    assert result.usage.consistency_rejections == 3


def test_consistency_rejection_then_valid_recovers_with_count_preserved():
    rubric = _binary_rubric()
    client = ScriptedClient(
        [
            [("submit_report", _contradicting_submit(refund_done=True))],  # rejected
            [("submit_report", _submit_args(refund_done=True))],  # corrected
        ]
    )
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)
    assert result.usage.consistency_rejections == 1


def test_malformed_then_valid_recovers():
    rubric = _binary_rubric()
    client = ScriptedClient(
        [
            [("submit_report", _submit_args(refund_done="yes"))],  # invalid
            [("submit_report", _submit_args(refund_done=True))],  # corrected
        ]
    )
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)


def test_weighted_score_and_criterion_results():
    rubric = _two_criteria_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True, tone=0.4))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    # (2*1.0 + 1*0.4) / 3 = 0.8
    assert result.score == pytest.approx(0.8)
    assert {cr.id for cr in result.criterion_results} == {"refund_done", "tone"}
    assert not result.gate_failed


def test_failed_required_criterion_gates_regardless_of_weighted_score():
    rubric = _two_criteria_rubric(required=True)
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=False, tone=1.0))]])
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    assert result.gate_failed is True
    assert result.binary_pass is False
    assert "refund_done" in result.failed_required_ids


def test_usage_is_recorded():
    rubric = _binary_rubric()
    client = ScriptedClient(
        [
            [("get_db_state", {})],
            [("submit_report", _submit_args(refund_done=True))],
        ]
    )
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert result.usage.calls == 2
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 10
    # submit_report terminates the loop before it executes as a tool, so only
    # the real read-tool (get_db_state) is counted.
    assert result.usage.tool_calls == 1
    assert result.usage.cost_usd == pytest.approx(0.002)


def test_turn_exhaustion_without_submit_report_errors():
    rubric = _binary_rubric()
    client = ScriptedClient([[("get_db_state", {})]] * 50)
    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        max_turns=3,
        llm_client=client,
    )
    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert client.calls == 3
    # Stage 5: an ERRORED judge still carries its partial transcript — the key
    # artifact for debugging WHY it failed.
    assert result.transcript


def test_judge_loop_crash_errors_not_scores():
    rubric = _binary_rubric()

    class BoomClient:
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            raise RuntimeError("provider exploded")

    result = run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        max_turns=3,
        llm_client=BoomClient(),
    )
    assert result.status is JudgeStatus.ERRORED
    assert result.score is None


def test_agent_system_prompt_injected_into_opening_context():
    rubric = _binary_rubric()
    captured: dict = {}

    class CapturingClient(ScriptedClient):
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            captured.setdefault("first_user", messages[0].content)
            return super().generate(system, messages, tools, tool_choice)

    client = CapturingClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="SECRET-AGENT-POLICY-MARKER",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert "SECRET-AGENT-POLICY-MARKER" in captured["first_user"]
    assert "do the thing" in captured["first_user"]


def test_state_diff_injected_into_opening_context():
    """The initial→final state diff is injected as the judge's primary view."""
    rubric = _binary_rubric()
    captured: dict = {}

    class CapturingClient(ScriptedClient):
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            captured.setdefault("first_user", messages[0].content)
            return super().generate(system, messages, tools, tool_choice)

    client = CapturingClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        state_diff="STATE-DIFF-MARKER: orders: 1 modified",
        llm_client=client,
    )
    assert "STATE-DIFF-MARKER" in captured["first_user"]


def test_input_surface_excludes_oracle_fields():
    """Narrow input surface: there is no parameter through which golden_actions /
    expected_hash / jsonpath_checks could leak into the judge."""
    import inspect

    params = set(inspect.signature(run_rubric_judge).parameters)
    for forbidden in ("golden_actions", "expected_hash", "jsonpath_checks", "grading_config"):
        assert forbidden not in params
