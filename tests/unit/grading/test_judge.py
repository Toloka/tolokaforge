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
from tolokaforge.core.models import ToolCall
from tolokaforge.runner.models import Rubric

pytestmark = pytest.mark.unit


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

    def generate(self, system, messages, tools, tool_choice="auto") -> GenerationResult:
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


def _submit_args(**criteria) -> dict:
    """Build a well-formed submit_report payload from {id: verdict} kwargs."""
    args: dict = {"reasons": "overall summary"}
    for cid, verdict in criteria.items():
        args[cid] = verdict
        args[f"{cid}_justification"] = f"because {cid}"
    return args


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_terminates_on_submit_report_and_scores():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])

    result = run_rubric_judge(
        rubric=rubric,
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
        agent_system_prompt="policy",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=reader,
        llm_client=client,
    )

    assert result.status is JudgeStatus.COMPLETED
    assert reader.get_state_calls == 1
    assert "get_db_state" in client.seen_tool_names
    assert "submit_report" in client.seen_tool_names


def test_db_tools_absent_when_no_reader():
    rubric = _binary_rubric()
    client = ScriptedClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        model_ref="openai/gpt-4o-mini",
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
        def generate(self, system, messages, tools, tool_choice="auto"):
            raise RuntimeError("provider exploded")

    result = run_rubric_judge(
        rubric=rubric,
        model_ref="openai/gpt-4o-mini",
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
        def generate(self, system, messages, tools, tool_choice="auto"):
            captured.setdefault("first_user", messages[0].content)
            return super().generate(system, messages, tools, tool_choice)

    client = CapturingClient([[("submit_report", _submit_args(refund_done=True))]])
    run_rubric_judge(
        rubric=rubric,
        model_ref="openai/gpt-4o-mini",
        agent_system_prompt="SECRET-AGENT-POLICY-MARKER",
        transcript=[{"role": "user", "content": "do the thing"}],
        db_reader=FakeDBReader(),
        llm_client=client,
    )
    assert "SECRET-AGENT-POLICY-MARKER" in captured["first_user"]
    assert "do the thing" in captured["first_user"]


def test_input_surface_excludes_oracle_fields():
    """Narrow input surface: there is no parameter through which golden_actions /
    expected_hash / jsonpath_checks could leak into the judge."""
    import inspect

    params = set(inspect.signature(run_rubric_judge).parameters)
    for forbidden in ("golden_actions", "expected_hash", "jsonpath_checks", "grading_config"):
        assert forbidden not in params
