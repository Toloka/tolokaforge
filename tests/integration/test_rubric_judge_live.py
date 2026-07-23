"""Live end-to-end test of the read-only rubric judge against a real LLM.

Runs ``LLMJudge`` over a small rubric + a canned transcript + a tiny
in-memory DB state, using a cheap real model through the agent's
``LLMClient`` / ``build_capabilities`` path (so tool schemas/calls are
provider-correct). The judge is agentic and its tool-call ordering is not
deterministic even at temperature 0, so this is an integration test gated on a
provider key rather than a snapshot — the deterministic orchestration contract
is pinned by ``tests/unit/grading/test_judge.py`` with a scripted client.

Run with:
    scripts/with_env.sh uv run pytest tests/integration/test_rubric_judge_live.py \\
        -q -m integration
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.grading.judge import (
    JudgeStatus,
    LLMJudge,
    model_config_from_ref,
)
from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.grading.rubric import SubmitReportValidationError, parse_submit_report
from tolokaforge.runner.models import Rubric

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.llm,
]

#: Golden well-formed submit_report payload captured from a real judge run. The
#: unit test ``test_rubric.py::TestWellFormedLivePayload`` re-validates it with no
#: spend. Regenerate by setting ``TF_CAPTURE_JUDGE_PAYLOAD=1`` when running the
#: acceptance test below (see ``tests/README.md``).
_WELLFORMED_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "unit"
    / "grading"
    / "data"
    / "wellformed_submit_report.json"
)


def _pick_model() -> str | None:
    """Cheap, tool-calling-capable model whose key is available.

    ``gpt-4.1-mini`` is deliberately chosen over the cheaper ``gpt-4o-mini``:
    the latter over-queries the read tools and fails to recognise it has enough
    evidence to submit, looping to MAX_TURNS on agentic grading. ``4.1-mini``
    is still cheap but reliably terminates.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter/openai/gpt-4.1-mini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai/gpt-4.1-mini"
    return None


_MODEL_REF = _pick_model()

# A tiny final DB state the judge can inspect via get_db_state / query_db.
_DB_STATE = {
    "orders": [
        {"id": "o_1001", "status": "refunded", "refund_amount": 328.50},
    ]
}

# A canned agent transcript: the agent issued the refund and reported the amount.
_TRANSCRIPT = [
    {"role": "user", "content": "I want to cancel order o_1001 and get my money back."},
    {
        "role": "assistant",
        "content": "I've processed your refund of $328.50 to your original payment method.",
    },
]

_AGENT_SYSTEM_PROMPT = (
    "You are a customer-service agent. Issue refunds when a cancellation is valid "
    "and always quote the exact refund amount to the customer."
)


class _DictDBReader:
    """In-memory read-only DB reader over a fixed final state.

    ``query`` evaluates the JSONPath against the state (mirroring the production
    DB service) so a filtering judge gets real per-field answers, not the whole
    blob — otherwise a model querying ``$.orders[?(@.id=='x')].status`` cannot
    extract the value and loops.
    """

    def __init__(self, state: dict):
        self._state = state

    def get_state(self, tables=None):
        if tables:
            return {t: self._state.get(t, []) for t in tables}
        return self._state

    def query(self, jsonpath):
        from jsonpath_ng.ext import parse

        matches = [m.value for m in parse(jsonpath).find(self._state)]
        return {"results": matches}


def _rubric() -> Rubric:
    return Rubric(
        reference="The correct refund for order o_1001 is $328.50 and it must be issued.",
        criteria=[
            {
                "id": "refund_issued",
                "description": "The order's refund was actually issued (status refunded).",
                "kind": "binary",
                "required": True,
                "weight": 1.0,
            },
            {
                "id": "amount_quoted",
                "description": "The agent quoted the correct refund amount to the customer.",
                "expected": "$328.50",
                "kind": "binary",
                "weight": 1.0,
            },
            {
                "id": "tone",
                "description": "The reply is polite and professional.",
                "kind": "graded",
                "weight": 0.5,
            },
        ],
    )


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_passes_good_transcript():
    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
    )

    # The judge must COMPLETE (not error) and produce one result per criterion.
    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert {cr.id for cr in result.criterion_results} == {
        "refund_issued",
        "amount_quoted",
        "tone",
    }
    # The state shows a refunded order at $328.50 and the agent quoted it — the
    # required criterion must pass, so the gate must NOT fire and the score high.
    assert result.gate_failed is False
    assert result.score is not None and result.score >= 0.7
    # Judge usage was recorded.
    assert result.usage.calls >= 1
    assert result.usage.prompt_tokens > 0


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_with_state_diff_injected():
    """The diff-first default: a rendered initial→final diff is injected as the
    judge's primary view. Exercises the ``state_diff`` param through the real
    provider path (real tool schemas / model), on top of the deterministic
    injection pinned by ``test_judge.py::test_state_diff_injected_into_opening_context``.
    """
    from tolokaforge.core.grading.state_diff import render_state_diff

    initial = {"orders": [{"id": "o_1001", "status": "pending", "refund_amount": 0.0}]}
    state_diff = render_state_diff(
        initial,
        _DB_STATE,
        primary_keys={"orders": "id"},
    )
    # Sanity: the renderer produced the transition the judge should grade on.
    assert "status:" in state_diff and "refunded" in state_diff

    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
        state_diff=state_diff,
    )

    # Passing the diff must not break the live path: judge completes, gate holds,
    # and the diff is captured verbatim into the judge's own transcript (the
    # inject+persist contract — the opening message is message[0]).
    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert result.gate_failed is False
    assert result.score is not None and result.score >= 0.7
    opening = result.transcript[0]["content"]
    assert "STATE CHANGES" in opening


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_gate_fails_when_refund_missing():
    # Final state shows the order still pending — the required criterion fails.
    pending_state = {"orders": [{"id": "o_1001", "status": "pending", "refund_amount": 0.0}]}
    transcript = [
        {"role": "user", "content": "Cancel order o_1001 and refund me."},
        {"role": "assistant", "content": "Sorry, I cannot process that right now."},
    ]
    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=transcript,
        db_reader=_DictDBReader(pending_state),
    )

    assert result.status is JudgeStatus.COMPLETED, result.reasons
    # refund_issued is required and the state shows no refund → gate fails.
    assert result.gate_failed is True
    assert "refund_issued" in result.failed_required_ids
    assert result.binary_pass is False


class _RealDbServiceReader:
    """Read-only DBReader backed by the REAL json_db_service via its HTTP API.

    Unlike ``_DictDBReader`` (which evaluates JSONPath locally with
    ``jsonpath_ng.ext``), this drives the actual service ``/query`` endpoint, so
    the judge's queries hit the same parser production uses. Mirrors the
    production ``DBServiceClient`` contract: a non-200 response raises, which
    ``QueryDbTool`` surfaces to the judge as a tool error. Every query is
    recorded so the test can assert the real parser never rejected one.

    This is the seam the bug in PR #157 slipped through: the old live test used
    an in-memory reader, so a DB-service-side JSONPath parser bug was invisible.
    """

    def __init__(self, test_client, trial_id: str):
        self._client = test_client
        self._trial_id = trial_id
        self.queries: list[tuple[str, int, str]] = []  # (jsonpath, status, detail)

    def get_state(self, tables=None):
        params = {"tables": ",".join(tables)} if tables else {}
        resp = self._client.get(f"/trials/{self._trial_id}/state", params=params)
        resp.raise_for_status()
        return resp.json()["data"]

    def query(self, jsonpath):
        resp = self._client.post(f"/trials/{self._trial_id}/query", json={"jsonpath": jsonpath})
        detail = "" if resp.status_code == 200 else resp.json().get("detail", resp.text)
        self.queries.append((jsonpath, resp.status_code, detail))
        if resp.status_code != 200:
            raise RuntimeError(f"query_db failed: {detail}")
        return {"results": resp.json()["results"]}


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_against_real_db_service(db_test_client):
    """End-to-end: real LLM judge driving the REAL DB service query endpoint.

    Regression guard for PR #157 — the judge's natural "look up an entity by id"
    move (``$.orders[?(@.id=="...")]``) must succeed against the real service.
    The previous live test backed the judge with an in-memory reader, so a
    service-side parser bug (base ``jsonpath_ng`` rejecting filter expressions)
    reached production undetected.
    """
    trial_id = "judge_live_dbsvc"
    # Several orders so the judge filters by id rather than dumping the table.
    tables = {
        "orders": [
            {"id": "o_1001", "status": "refunded", "refund_amount": 328.50},
            {"id": "o_1002", "status": "pending", "refund_amount": 0.0},
            {"id": "o_1003", "status": "shipped", "refund_amount": 0.0},
        ]
    }
    init = db_test_client.post(f"/trials/{trial_id}/init", json={"tables": tables})
    assert init.status_code == 200, init.text

    reader = _RealDbServiceReader(db_test_client, trial_id)
    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=reader,
    )

    # The judge grades correctly against the real service (o_1001 is refunded).
    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert result.gate_failed is False

    # The load-bearing guard (never flaky): no query the judge issued was
    # rejected by the real parser. Under the pre-#157 bug, every `[?(...)]`
    # filter returned 400 "Unexpected character: ?" — this would fail.
    parse_failures = [q for q in reader.queries if q[1] != 200]
    assert not parse_failures, f"real DB service rejected judge queries: {parse_failures}"

    # Deterministic belt-and-suspenders: exercise a filter expression through the
    # real service directly, so the regression is caught even on a run where the
    # model happened to only use `[*]` wildcards.
    direct = db_test_client.post(
        f"/trials/{trial_id}/query",
        json={"jsonpath": '$.orders[?(@.id=="o_1001")].status'},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["results"] == ["refunded"]


# ---------------------------------------------------------------------------
# Marker-consistency acceptance — real providers across judge tiers/families
# ---------------------------------------------------------------------------


def _acceptance_models() -> list[tuple[str, str]]:
    """(label, model_ref) pairs for the marker-acceptance run, gated on keys.

    Mid: a mid-tier GPT-class model that reliably terminates the agentic
    grading loop — also the golden-fixture capture source. Weak: a smaller,
    different-family OpenRouter model that stresses the marker format
    instruction. Strong: the Anthropic judge model the committed rejection
    fixtures came from (``tests/unit/grading/data/README.md``). It is routed
    via OpenRouter because the judge pins ``temperature=0.0`` and the native
    Anthropic API rejects sampling params on Opus 4.8, while OpenRouter strips
    them. All three ride a single OpenRouter key.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        return []
    return [
        ("mid", "openrouter/openai/gpt-4.1-mini"),
        ("weak", "openrouter/meta-llama/llama-3.3-70b-instruct"),
        ("strong", "openrouter/anthropic/claude-opus-4.8"),
    ]


def _extract_submit_args(result) -> dict | None:
    """The LAST submit_report arguments the judge emitted, from its transcript.

    On a retried run earlier submit_report calls are the REJECTED payloads; the
    final one is the accepted, well-formed payload the golden fixture wants.
    """
    args = None
    for msg in result.transcript:
        for tc in msg.get("tool_calls") or []:
            if tc.get("name") == "submit_report":
                args = tc.get("arguments")
    return args


@pytest.mark.skipif(not _acceptance_models(), reason="No OPENROUTER_API_KEY set")
@pytest.mark.parametrize("label,model_ref", _acceptance_models(), ids=lambda v: v)
def test_rubric_judge_live_markers_match_verdicts(label: str, model_ref: str):
    """A well-formed transcript → COMPLETED with every justification's marker
    matching its verdict and no consistency rejections, across judge tiers and
    families (mid GPT-class, weak open-weights, strong Anthropic-family).

    Set ``TF_CAPTURE_JUDGE_PAYLOAD=1`` to (re)capture the mid model's real
    well-formed submit_report payload into the golden fixture the unit acceptance
    test re-validates without spend.
    """
    rubric = _rubric()
    judge = LLMJudge(model_config_from_ref(model_ref), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=rubric,
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
    )

    assert result.status is JudgeStatus.COMPLETED, result.reasons
    # No submit_report attempt was rejected for a verdict/justification mismatch.
    assert result.usage.consistency_rejections == 0, result.reasons
    # Every criterion carries a justification whose trailing marker matches its
    # verdict — re-validated independently through parse_submit_report (raises
    # VerdictConsistencyError on any mismatch).
    replay: dict = {"reasons": result.reasons}
    kinds = {c.id: c.kind for c in rubric.criteria}
    for cr in result.criterion_results:
        assert cr.justification.strip(), f"{cr.id} justification empty"
        replay[f"{cr.id}_justification"] = cr.justification
        replay[cr.id] = cr.met if kinds[cr.id] == "binary" else cr.score
    parse_submit_report(replay, rubric)  # must not raise

    if label == "mid" and os.environ.get("TF_CAPTURE_JUDGE_PAYLOAD") == "1":
        captured = _extract_submit_args(result)
        assert captured is not None, "no submit_report call found in transcript"
        _WELLFORMED_FIXTURE.write_text(json.dumps(captured, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Retry recovery — OpenAI-family judge survives a rejected submit_report
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_recovers_through_forced_retry(monkeypatch):
    """A real OpenAI-family judge recovers through the ``submit_report`` retry.

    The model's tool call is 100% real on both turns — the only thing forced is
    the harness's *validation decision*: ``parse_submit_report`` (as bound in the
    judge module) rejects the first real payload exactly once, then delegates to
    the real validator. This is the path a genuine weak-model miss takes: real
    model → first submit_report → rejection → repaired retry sequence → live
    provider round-trip → recovery. The assertion under test — the live provider
    accepts the repaired tool-call/tool-result sequence rather than 400-ing on an
    unanswered ``tool_call_id`` — is fully real.
    """
    from tolokaforge.core.grading import judge as judge_module

    real_parse = judge_module.parse_submit_report
    parse_calls = {"n": 0}

    def _reject_first_then_delegate(tool_args, rubric):
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            raise SubmitReportValidationError(
                "forced rejection to exercise the retry path (test only)"
            )
        return real_parse(tool_args, rubric)

    monkeypatch.setattr(judge_module, "parse_submit_report", _reject_first_then_delegate)

    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
    )

    # The first real submit_report was rejected, and a second generation reached
    # the validator — i.e. the live provider accepted the repaired retry sequence.
    # A 400 on the retry would raise a BadRequestError the judge catches and turns
    # into ERRORED before parse is called a second time, so this only holds when
    # the repaired sequence was accepted.
    assert parse_calls["n"] >= 2, result.reasons
    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert result.score is not None
    # The forced rejection is a generic SubmitReportValidationError, not a
    # VerdictConsistencyError, so the consistency counter is unaffected.
    assert result.usage.consistency_rejections == 0
    # The injected rejection is preserved in the audit transcript as the tool
    # result for the rejected submit_report call.
    rejection_results = [
        m
        for m in result.transcript
        if m.get("role") == "tool"
        and m.get("tool_call_id")
        and "rejected" in (m.get("content") or "")
    ]
    assert rejection_results, "no injected rejection role=tool result in the transcript"


# ---------------------------------------------------------------------------
# Judge customization acceptance — disable_knowledge_search end-to-end
# ---------------------------------------------------------------------------


class _StubKnowledgeSearch:
    """In-memory ``KnowledgeSearch`` giving the judge a KB tool to offer/withhold.

    The LLM is real; the KB backend is a stub (mirroring ``_DictDBReader`` for the
    DB). Its presence is what matters: with the flag off the judge is offered
    ``search_kb``; with it on the judge withholds it, so ``withheld`` is non-empty.
    """

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        return [
            SearchHit(
                doc_id="policy_1",
                source="refund_policy.md",
                score=0.9,
                text="Refunds are issued to the original payment method within 30 days.",
            )
        ]


def _tool_names_in_transcript(transcript) -> list[str]:
    return [tc["name"] for m in transcript for tc in (m.get("tool_calls") or [])]


def _write_live_grade(tmp_path: Path, result) -> tuple[dict, dict]:
    """Materialise the live judge result into real ``grade.yaml`` +
    ``judge_trajectory.yaml`` via the production :class:`FileArtifactWriter`, then
    read both back. The proto→dict→Grade field mapping is contract-locked in
    ``tests/canonical/test_trial_grader_contract.py``; here we assert the written
    bundle carries the live judge's gating record."""
    from tolokaforge.core.models import Grade, GradeComponents, JudgeKbGating
    from tolokaforge.core.models import JudgeStatus as HostJudgeStatus
    from tolokaforge.core.models import JudgeUsage as HostJudgeUsage
    from tolokaforge.core.output.artifacts import FileArtifactWriter

    grade = Grade(
        binary_pass=bool(result.binary_pass),
        score=result.score if result.score is not None else 0.0,
        components=GradeComponents(llm_judge=result.score if result.score is not None else -1.0),
        reasons=result.reasons,
        criterion_results=list(result.criterion_results),
        judge_status=HostJudgeStatus(result.status.value),
        judge_usage=HostJudgeUsage(
            calls=result.usage.calls,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            reasoning_tokens=result.usage.reasoning_tokens,
            cost_usd=result.usage.cost_usd,
            tool_calls=result.usage.tool_calls,
            consistency_rejections=result.usage.consistency_rejections,
        ),
        judge_transcript=list(result.transcript),
        judge_kb_gating=JudgeKbGating(
            knowledge_search_disabled=result.knowledge_search_disabled,
            offered=list(result.kb_tools_offered),
            withheld=list(result.kb_tools_withheld),
        ),
        judge_agent_prompt_included=result.include_agent_system_prompt,
    )
    trial_dir = tmp_path / "trials" / "judge_customization" / "0"
    FileArtifactWriter().write_grade(trial_dir, grade)
    grade_yaml = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    traj_yaml = yaml.safe_load((trial_dir / "judge_trajectory.yaml").read_text())
    return grade_yaml, traj_yaml


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_judge_customization_disabled(tmp_path):
    """With ``disable_knowledge_search=True`` the real judge grades end-to-end with
    NO KB tool in its surface: none offered, ``search_kb`` withheld, no KB call in
    the trajectory, and ``grade.yaml`` records the gating (the authoritative
    replay signal)."""
    judge = LLMJudge(
        model_config_from_ref(_MODEL_REF),
        max_turns=10,
        episode_timeout_s=180,
        disable_knowledge_search=True,
    )
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
        kb_search=_StubKnowledgeSearch(),
    )

    # Grading completes end-to-end without the KB tool.
    assert result.status is JudgeStatus.COMPLETED, result.reasons
    # No KB tool in the judge's toolset; the agent's search_kb was withheld.
    assert result.kb_tools_offered == ()
    assert "search_kb" in result.kb_tools_withheld
    assert result.knowledge_search_disabled is True
    # No KB search call in the judge's trajectory (the tool was never offered).
    assert "search_kb" not in _tool_names_in_transcript(result.transcript)

    grade_yaml, traj_yaml = _write_live_grade(tmp_path, result)
    gating = grade_yaml["judge_kb_gating"]
    assert gating["knowledge_search_disabled"] is True
    assert gating["offered"] == []
    assert gating["withheld"]  # non-empty — the agent had a KB tool that was withheld
    assert "search_kb" not in _tool_names_in_transcript(traj_yaml["messages"])


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_judge_customization_baseline(tmp_path):
    """Without the flag the judge is offered the SAME KB tool the agent had —
    the faithful baseline: ``search_kb`` offered, nothing withheld, and
    ``grade.yaml`` records ``knowledge_search_disabled: false``."""
    judge = LLMJudge(model_config_from_ref(_MODEL_REF), max_turns=10, episode_timeout_s=180)
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
        kb_search=_StubKnowledgeSearch(),
    )

    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert result.kb_tools_offered == ("search_kb",)
    assert result.kb_tools_withheld == ()
    assert result.knowledge_search_disabled is False

    grade_yaml, _ = _write_live_grade(tmp_path, result)
    gating = grade_yaml["judge_kb_gating"]
    assert gating["knowledge_search_disabled"] is False
    assert gating["offered"] == ["search_kb"]
    assert gating["withheld"] == []


# ---------------------------------------------------------------------------
# Judge customization acceptance — include_agent_system_prompt end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_MODEL_REF is None, reason="No OPENROUTER_API_KEY / OPENAI_API_KEY set")
def test_rubric_judge_live_agent_prompt_gating_omits_policy(tmp_path):
    """With ``include_agent_system_prompt=False`` the real judge grades end-to-end
    with NO agent-policy section in its opening-message evidence: grading completes,
    no consistency rejections, the recorded ``judge_trajectory.yaml`` opening message
    carries neither the policy-framing sentence nor the agent prompt text, and
    ``grade.yaml`` records ``judge_agent_prompt_included: false``."""
    judge = LLMJudge(
        model_config_from_ref(_MODEL_REF),
        max_turns=10,
        episode_timeout_s=180,
        include_agent_system_prompt=False,
    )
    result = judge.run(
        rubric=_rubric(),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
    )

    assert result.status is JudgeStatus.COMPLETED, result.reasons
    assert result.usage.consistency_rejections == 0, result.reasons
    assert result.include_agent_system_prompt is False

    grade_yaml, traj_yaml = _write_live_grade(tmp_path, result)
    assert grade_yaml["judge_agent_prompt_included"] is False
    opening = traj_yaml["messages"][0]["content"]
    assert "The agent under evaluation operated under this policy" not in opening
    assert _AGENT_SYSTEM_PROMPT not in opening
