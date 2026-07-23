"""Deterministic orchestration tests for the read-only rubric judge.

These drive the REAL ``LLMJudge`` / ``ToolCallingLoop`` / ``rubric.py``
with a SCRIPTED ``LoopLLMClient`` — a fake that returns pre-set tool calls. That
is the right level: it tests OUR orchestration (termination on submit_report,
bounded re-prompt, gating, usage accounting, fail-loud), not the LLM. The DB
reader is a tiny in-memory fake exercising the read-only tool path.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge import (
    _JUDGE_MARKER_CONTRACT,
    _JUDGE_SYSTEM_PROMPT,
    JudgeStatus,
    LLMJudge,
    _build_opening_message,
    _compose_judge_system_prompt,
)
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall
from tolokaforge.runner.models import Rubric

pytestmark = pytest.mark.unit

# The judge now takes a run-level ModelConfig. These tests inject a scripted
# ``llm_client`` so the config is never used to build a real client; it is
# supplied only to satisfy the required signature.
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)

#: Construction-time kwargs of ``LLMJudge`` — everything else is per-trial evidence.
_LLM_JUDGE_CTOR_KEYS = (
    "model_config",
    "llm_client",
    "max_turns",
    "episode_timeout_s",
    "submit_report_retries",
    "disable_knowledge_search",
    "custom_system_prompt",
    "include_agent_system_prompt",
    "logger",
)


def _run_llm_judge(**kwargs):
    """Drive ``LLMJudge`` from one flat kwargs dict.

    Splits the construction-time config (``model_config``, injected ``llm_client``,
    the budgets) from the per-trial evidence surface so each test keeps a single
    call and the behaviour under test is identical to the driven ``LLMJudge.run``.
    """
    ctor_kwargs = {k: kwargs.pop(k) for k in _LLM_JUDGE_CTOR_KEYS if k in kwargs}
    model_config = ctor_kwargs.pop("model_config")
    return LLMJudge(model_config, **ctor_kwargs).run(**kwargs)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedClient:
    """A scripted ``LoopLLMClient``: returns queued GenerationResults in order.

    Each entry is either a list of ``(tool_name, arguments)`` tuples (emitted as
    tool calls) or a plain string (assistant text, no tool calls). Records the
    system prompt/tools/messages it was driven with for assertions.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.seen_tool_names: list[str] = []
        self.seen_system: str | None = None

    def generate(
        self, system, messages, tools, tool_choice="auto", observation=None
    ) -> GenerationResult:
        self.calls += 1
        self.seen_system = system
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


class MessageCapturingClient(ScriptedClient):
    """A ``ScriptedClient`` that snapshots the ``messages`` list of each call.

    Each snapshot is a shallow copy taken at generation time; the judge only
    del/appends whole ``Message`` objects (never mutates one in place) on the
    retry path, so a per-call shallow copy faithfully freezes what that
    generation was handed. ``snapshots[1]`` is the list fed to the first retry.
    """

    def __init__(self, script: list):
        super().__init__(script)
        self.snapshots: list[list[Message]] = []

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.snapshots.append(list(messages))
        return super().generate(system, messages, tools, tool_choice, observation)


def _assert_strong_adjacency(messages: list[Message]) -> None:
    """Every ``tool_call_id`` on every assistant message is answered by a
    ``role=tool`` result in the contiguous block immediately following it.

    This is the provider wire contract (OpenAI/Azure 400 otherwise): no non-tool
    message may sit between an assistant tool-call message and the tool results
    answering its ids.
    """
    for i, msg in enumerate(messages):
        if msg.role is not MessageRole.ASSISTANT or not msg.tool_calls:
            continue
        answered: list[str] = []
        j = i + 1
        while j < len(messages) and messages[j].role is MessageRole.TOOL:
            answered.append(messages[j].tool_call_id)
            j += 1
        for tc in msg.tool_calls:
            assert tc.id in answered, (
                f"tool_call_id {tc.id!r} on assistant message {i} is not answered by "
                f"an adjacent role=tool result (adjacent tool ids: {answered})"
            )


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

    result = _run_llm_judge(
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
    # The judge captures its own transcript for the audit bundle, incl. the
    # submit_report tool call.
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

    result = _run_llm_judge(
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
    _run_llm_judge(
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
    _run_llm_judge(
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

    result = _run_llm_judge(
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
    _run_llm_judge(
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
    _run_llm_judge(
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

    result = _run_llm_judge(
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
    result = _run_llm_judge(
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
        knowledge_search=True,
    )
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    result = _run_llm_judge(
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
    result = _run_llm_judge(
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
    result = _run_llm_judge(
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


# ---------------------------------------------------------------------------
# Construction-time KB gating: disable_knowledge_search withholds KB-tagged tools
# (classification is by the declared tag, never by tool name)
# ---------------------------------------------------------------------------


def _kb_tagged_passthrough(name: str = "search_policy"):
    """A ``search_policy``-style passthrough tagged as knowledge-search (as the
    runner tags it in ``_build_judge_search_policy_tools``)."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    return DelegatingReadTool(
        name=name,
        description="Search the policy KB",
        parameters=_search_policy_schema(),
        invoke=lambda args: "policy result text",
        knowledge_search=True,
    )


def test_disable_knowledge_search_withholds_kb_tagged_tools():
    """disable_knowledge_search=True withholds EVERY KB-tagged tool from the judge's
    schema (rag search_kb + the tagged passthrough), records them in withheld with
    an empty offered set and the flag True, and leaves a non-KB extra read tool
    registered."""
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool

    rubric = _binary_rubric()
    non_kb = DelegatingReadTool(
        name="read_ledger",
        description="Read the audit ledger",
        parameters=_search_policy_schema(),
        invoke=lambda args: "ledger",
    )  # untagged → not knowledge-search → must never be gated
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])

    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=FakeKnowledgeSearch(),
        extra_read_tools=[_kb_tagged_passthrough(), non_kb],
        disable_knowledge_search=True,
        llm_client=client,
    )

    # Neither KB-tagged tool ever entered the schema handed to the LLM.
    assert "search_kb" not in client.seen_tool_names
    assert "search_policy" not in client.seen_tool_names
    # The non-KB read tool is untouched — the agent-mirroring read surface stays —
    # and it is recorded in the replayable read surface alongside the DB tools.
    assert "read_ledger" in client.seen_tool_names
    assert result.read_tools_offered == ("get_db_state", "query_db", "read_ledger")
    assert result.kb_tools_offered == ()
    assert result.kb_tools_withheld == ("search_kb", "search_policy")
    assert result.knowledge_search_disabled is True
    assert "Judge KB: none offered (disabled by config)" in result.reasons


def test_disable_flag_false_is_byte_for_byte_default():
    """disable_knowledge_search=False (the default) leaves the offered set, the
    (empty) withheld set, and the reasons note identical to the ungated judge."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])

    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=FakeKnowledgeSearch(),
        extra_read_tools=[_kb_tagged_passthrough()],
        disable_knowledge_search=False,
        llm_client=client,
    )

    assert "search_kb" in client.seen_tool_names
    assert "search_policy" in client.seen_tool_names
    assert result.kb_tools_offered == ("search_kb", "search_policy")
    assert result.kb_tools_withheld == ()
    assert result.knowledge_search_disabled is False
    assert "Judge KB: search_kb, search_policy" in result.reasons


def test_disable_flag_true_but_no_kb_records_flag_with_empty_withheld():
    """A disabled judge over a KB-less trial records the flag True with an EMPTY
    withheld set (nothing to gate) and the plain 'none offered' note — the
    config-disable-vs-faithful-none distinction the gating record must preserve."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])

    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        kb_search=None,
        extra_read_tools=None,
        disable_knowledge_search=True,
        llm_client=client,
    )

    assert result.kb_tools_offered == ()
    assert result.kb_tools_withheld == ()
    assert result.knowledge_search_disabled is True
    assert "Judge KB: none offered" in result.reasons
    assert "(disabled by config)" not in result.reasons


def test_db_tools_absent_when_no_reader():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    _run_llm_judge(
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
    """Invalid submit_report → bounded re-prompt → ERRORED with NO score.

    Every retry generation — including the second, whose surgery rewrites an
    already-rewritten message list — is handed a provider-valid tail.
    """
    rubric = _binary_rubric()
    bad = _submit_args(refund_done="yes")  # binary expects a bool
    client = MessageCapturingClient(
        [
            [("submit_report", bad)],
            [("submit_report", bad)],
            [("submit_report", bad)],
        ]
    )

    result = _run_llm_judge(
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
    for snap in client.snapshots[1:]:
        _assert_strong_adjacency(snap)


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


@pytest.mark.parametrize(
    "bad_submit",
    [_contradicting_submit, _missing_marker_submit],
    ids=["contradicting_marker", "missing_marker"],
)
def test_consistency_rejection_reprompts_and_counts_per_attempt_then_errors(bad_submit):
    """Consistency-rejected payload → re-prompt; counter increments per rejected
    attempt; after retry exhaustion the result is ERRORED with the count
    reflecting attempts."""
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", bad_submit(refund_done=True))]] * 3)
    result = _run_llm_judge(
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


def test_consistency_rejection_then_valid_recovers_with_count_preserved():
    rubric = _binary_rubric()
    client = ScriptedClient(
        [
            [("submit_report", _contradicting_submit(refund_done=True))],  # rejected
            [("submit_report", _submit_args(refund_done=True))],  # corrected
        ]
    )
    result = _run_llm_judge(
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
    result = _run_llm_judge(
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


@pytest.mark.parametrize(
    "first_step",
    [
        [("submit_report", _submit_args(refund_done="yes"))],  # schema (met not a bool)
        [("submit_report", _contradicting_submit(refund_done=True))],  # consistency marker
    ],
    ids=["schema_rejection", "consistency_rejection"],
)
def test_retry_messages_are_provider_valid(first_step):
    """After a rejected submit_report, the retry generation is handed a
    provider-valid sequence: the terminating assistant message's submit_report id
    is answered by an adjacent role=tool result carrying the validation error, and
    no non-tool message interleaves. Locked for both rejection classes.
    """
    rubric = _binary_rubric()
    client = MessageCapturingClient(
        [first_step, [("submit_report", _submit_args(refund_done=True))]]
    )
    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    retry_messages = client.snapshots[1]
    _assert_strong_adjacency(retry_messages)
    # The submit_report id's tool result carries the validation error + corrective.
    submit_id = "call_1_0"
    submit_results = [
        m for m in retry_messages if m.role is MessageRole.TOOL and m.tool_call_id == submit_id
    ]
    assert len(submit_results) == 1
    content = submit_results[0].content
    assert "rejected" in content
    assert "refund_done" in content  # the validation error names the criterion
    assert "call submit_report again" in content


def test_retry_answers_sibling_tool_call_ids():
    """A judge that emits a sibling read/search call alongside submit_report in the
    terminating turn must, on retry, have BOTH ids answered by adjacent role=tool
    results. A single-id fix leaves the sibling unanswered and a real provider
    400s.
    """
    rubric = _binary_rubric()
    client = MessageCapturingClient(
        [
            [
                ("query_db", {"jsonpath": "$.orders"}),
                ("submit_report", _submit_args(refund_done="yes")),
            ],
            [("submit_report", _submit_args(refund_done=True))],
        ]
    )
    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    retry_messages = client.snapshots[1]
    _assert_strong_adjacency(retry_messages)
    sibling_id, submit_id = "call_1_0", "call_1_1"
    answered = {m.tool_call_id: m.content for m in retry_messages if m.role is MessageRole.TOOL}
    assert sibling_id in answered and submit_id in answered
    # The sibling never ran (termination fired first) — an honest note, not output.
    assert "not executed" in answered[sibling_id]
    assert "rejected" in answered[submit_id]


def test_retry_rejection_appears_in_audit_transcript():
    """After a reject→recover run, the serialized transcript keeps the injected
    rejection role=tool entry — the audit crumb for the retry cycle.
    """
    rubric = _binary_rubric()
    client = ScriptedClient(
        [
            [("submit_report", _submit_args(refund_done="yes"))],  # rejected
            [("submit_report", _submit_args(refund_done=True))],  # corrected
        ]
    )
    result = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        db_reader=FakeDBReader(),
        submit_report_retries=2,
        llm_client=client,
    )
    assert result.status is JudgeStatus.COMPLETED
    tool_entries = [
        m
        for m in result.transcript
        if m["role"] == "tool" and "rejected" in (m.get("content") or "")
    ]
    assert len(tool_entries) == 1
    assert tool_entries[0]["tool_call_id"] == "call_1_0"


def test_weighted_score_and_criterion_results():
    rubric = _two_criteria_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True, tone=0.4))]])
    result = _run_llm_judge(
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
    result = _run_llm_judge(
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
    result = _run_llm_judge(
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
    # A clean run records zero verdict-consistency rejections.
    assert result.usage.consistency_rejections == 0


def test_turn_exhaustion_without_submit_report_errors():
    rubric = _binary_rubric()
    client = ScriptedClient([[("get_db_state", {})]] * 50)
    result = _run_llm_judge(
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
    # An ERRORED judge still carries its partial transcript — the key artifact
    # for debugging WHY it failed.
    assert result.transcript


def test_judge_loop_crash_errors_not_scores():
    rubric = _binary_rubric()

    class BoomClient:
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            raise RuntimeError("provider exploded")

    result = _run_llm_judge(
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
    _run_llm_judge(
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
    _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        state_diff="STATE-DIFF-MARKER: orders: 1 modified",
        llm_client=client,
    )
    assert "STATE-DIFF-MARKER" in captured["first_user"]


def test_opening_message_default_embeds_agent_policy_byte_for_byte():
    """The default embeds the agent-policy section, and passing the default kwarg
    explicitly changes nothing — byte-for-byte the ungated call."""
    transcript = [{"role": "user", "content": "do the thing"}]
    ungated = _build_opening_message("AGENT-POLICY-MARKER", transcript, "orders: 1 modified")
    explicit_default = _build_opening_message(
        "AGENT-POLICY-MARKER",
        transcript,
        "orders: 1 modified",
        include_agent_system_prompt=True,
    )
    assert ungated == explicit_default
    assert "The agent under evaluation operated under this policy" in ungated
    assert "AGENT-POLICY-MARKER" in ungated


def test_opening_message_gated_omits_agent_policy_section():
    """When gated, the agent-policy section is absent entirely — neither the
    policy-framing sentence nor the agent prompt text appears — while the
    transcript and state sections are untouched."""
    transcript = [{"role": "user", "content": "do the thing"}]
    gated = _build_opening_message(
        "AGENT-POLICY-MARKER",
        transcript,
        "orders: 1 modified",
        include_agent_system_prompt=False,
    )
    assert "The agent under evaluation operated under this policy" not in gated
    assert "AGENT-POLICY-MARKER" not in gated
    assert "do the thing" in gated
    assert "orders: 1 modified" in gated


# ---------------------------------------------------------------------------
# System-prompt contract tokens: the invariants a future reword must not drop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "VERDICT: MET",
        "VERDICT: NOT MET",
        "SCORE: <value in [0,1]>",
        "positive evidence",
        "the criterion FAILS",
    ],
    ids=[
        "marker_binary_met",
        "marker_binary_not_met",
        "marker_graded_score",
        "positive_evidence_rule",
        "absent_behavior_fails",
    ],
)
def test_judge_system_prompt_carries_contract_tokens(token):
    """The judge system prompt must carry the enforced marker tokens and the
    positive-evidence rule. The marker form is what ``parse_submit_report``
    validates (dropping it burns retries); the positive-evidence rule is the
    grading stance itself. The maintainer prose is theirs to reword — these
    tokens are the invariants a reword must preserve, so the lock scopes to them,
    not the full text.
    """
    assert token in _JUDGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Custom system prompt — body replacement with an always-appended marker
# ---------------------------------------------------------------------------

_MARKER_TOKENS = ("VERDICT: MET", "VERDICT: NOT MET", "SCORE:")


def test_compose_none_is_byte_for_byte_default():
    """No custom prompt yields the default prompt unchanged, byte-for-byte."""
    assert _compose_judge_system_prompt(None) == _JUDGE_SYSTEM_PROMPT


def test_compose_custom_body_replaces_and_appends_marker():
    """A custom body leads the prompt but cannot drop the marker contract: the
    composed prompt starts with the custom text and still carries the full marker
    contract with every enforced token."""
    composed = _compose_judge_system_prompt("Custom judge voice.")

    assert composed.startswith("Custom judge voice.")
    assert _JUDGE_MARKER_CONTRACT in composed
    for token in _MARKER_TOKENS:
        assert token in composed


@pytest.mark.parametrize("token", _MARKER_TOKENS)
def test_marker_contract_is_the_single_source_of_the_tokens(token):
    """The marker tokens live in ``_JUDGE_MARKER_CONTRACT`` — the one place the
    default and every custom prompt draw the enforced contract from."""
    assert token in _JUDGE_MARKER_CONTRACT


def test_custom_system_prompt_recorded_on_result():
    """A judge constructed with a custom prompt records ``custom_system_prompt``
    True on its result and sends the custom body at the head of the system prompt
    it puts on the wire; the default records False."""
    rubric = _binary_rubric()

    custom_client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    custom = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        custom_system_prompt="Grade only the refund.",
        llm_client=custom_client,
    )
    assert custom.custom_system_prompt is True
    assert custom_client.seen_system is not None
    assert custom_client.seen_system.startswith("Grade only the refund.")
    assert _JUDGE_MARKER_CONTRACT in custom_client.seen_system

    default = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="",
        transcript=[],
        llm_client=ScriptedClient([[("submit_report", _submit_args(refund_done=True))]]),
    )
    assert default.custom_system_prompt is False


def test_include_agent_system_prompt_recorded_on_result():
    """A judge constructed gated withholds the agent-policy section from the
    opening message it sends and records ``include_agent_system_prompt`` False on
    its result; default construction embeds the policy and records True."""
    rubric = _binary_rubric()

    gated_client = MessageCapturingClient([[("submit_report", _submit_args(refund_done=True))]])
    gated = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="SECRET-AGENT-POLICY-MARKER",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        include_agent_system_prompt=False,
        llm_client=gated_client,
    )
    assert gated.include_agent_system_prompt is False
    assert "SECRET-AGENT-POLICY-MARKER" not in gated_client.snapshots[0][0].content

    default = _run_llm_judge(
        rubric=rubric,
        model_config=_JUDGE_MODEL,
        agent_system_prompt="SECRET-AGENT-POLICY-MARKER",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        llm_client=ScriptedClient([[("submit_report", _submit_args(refund_done=True))]]),
    )
    assert default.include_agent_system_prompt is True
