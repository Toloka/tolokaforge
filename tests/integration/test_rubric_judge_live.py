"""Live end-to-end test of the read-only rubric judge against a real LLM.

Runs ``run_rubric_judge`` over a small rubric + a canned transcript + a tiny
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

import os

import pytest

from tolokaforge.core.grading.judge import (
    JudgeStatus,
    model_config_from_ref,
    run_rubric_judge,
)
from tolokaforge.runner.models import Rubric

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.llm,
]


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
    result = run_rubric_judge(
        rubric=_rubric(),
        model_config=model_config_from_ref(_MODEL_REF),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
        max_turns=10,
        episode_timeout_s=180,
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

    result = run_rubric_judge(
        rubric=_rubric(),
        model_config=model_config_from_ref(_MODEL_REF),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=_TRANSCRIPT,
        db_reader=_DictDBReader(_DB_STATE),
        state_diff=state_diff,
        max_turns=10,
        episode_timeout_s=180,
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
    result = run_rubric_judge(
        rubric=_rubric(),
        model_config=model_config_from_ref(_MODEL_REF),
        agent_system_prompt=_AGENT_SYSTEM_PROMPT,
        transcript=transcript,
        db_reader=_DictDBReader(pending_state),
        max_turns=10,
        episode_timeout_s=180,
    )

    assert result.status is JudgeStatus.COMPLETED, result.reasons
    # refund_issued is required and the state shows no refund → gate fails.
    assert result.gate_failed is True
    assert "refund_issued" in result.failed_required_ids
    assert result.binary_pass is False
