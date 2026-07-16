"""End-to-end calibration plumbing with a SCRIPTED judge client — no money.

Reuses the Stage-4 scripted ``LoopLLMClient`` approach (``tests/unit/grading/
test_judge.py``): the fake returns pre-set ``submit_report`` tool calls, so the
REAL judge loop + rubric parsing + calibration runner + metrics run end-to-end
deterministically. This tests OUR calibration plumbing (verdict pairing,
agreement numbers, disagreement extraction, gate), not the LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rubric_calibrator.fixture import GoldenFixture
from rubric_calibrator.metrics import decide_gate
from rubric_calibrator.runner import run_calibration

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import ToolCall

pytestmark = pytest.mark.unit


def _submit_schema(tools) -> dict:
    for tool in tools:
        if tool["function"]["name"] == "submit_report":
            return tool["function"]["parameters"]["properties"]
    raise AssertionError("submit_report not offered")


def _tool_call_result(args: dict) -> GenerationResult:
    return GenerationResult(
        text="",
        tool_calls=[ToolCall(id="call_1", name="submit_report", arguments=args)],
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        cost_usd=0.001,
    )


def _fixture(fid: str, *, expected: dict, db=None) -> GoldenFixture:
    # Embed a unique, judge-visible marker in the transcript so the shared
    # _RouterJudge can tell which fixture it is currently grading from the
    # opening user message (run_calibration shares one client across fixtures).
    return GoldenFixture(
        id=fid,
        rubric={
            "criteria": [
                {"id": "refund_issued", "description": "refund issued", "kind": "binary"},
                {"id": "tone", "description": "polite", "kind": "graded", "weight": 0.5},
            ]
        },
        agent_system_prompt="be nice",
        transcript=[{"role": "user", "content": f"refund me [FIXTURE={fid}]"}],
        final_db_state=db,
        expected=[
            {"criterion_id": cid, **({"met": v} if isinstance(v, bool) else {"score": v})}
            for cid, v in expected.items()
        ],
    )


def test_calibration_perfect_agreement_passes_gate():
    # Two fixtures; the scripted judge matches the human label on both.
    fixtures = [
        (Path("f1.yaml"), _fixture("f1", expected={"refund_issued": True, "tone": 0.9})),
        (Path("f2.yaml"), _fixture("f2", expected={"refund_issued": False, "tone": 0.2})),
    ]

    router = _RouterJudge(
        {
            "f1": {"refund_issued": True, "tone": 0.9},
            "f2": {"refund_issued": False, "tone": 0.1},
        }
    )
    run = run_calibration(fixtures, model_ref="x/y", llm_client=router)
    report = run.report

    assert report.total_observations == 4
    assert report.overall_accuracy == pytest.approx(1.0)
    assert not report.disagreements
    assert not report.has_errors
    gate = decide_gate(report, threshold=0.6, metric="kappa")
    assert gate.shippable is True
    # Usage was aggregated across both fixtures.
    assert run.total_usage.calls == 2
    assert run.total_usage.prompt_tokens == 20


def test_calibration_disagreement_drops_agreement_and_fails_gate():
    fixtures = [
        (Path("f1.yaml"), _fixture("f1", expected={"refund_issued": True, "tone": 0.9})),
        (Path("f2.yaml"), _fixture("f2", expected={"refund_issued": True, "tone": 0.9})),
    ]
    # Judge disagrees on refund_issued for both fixtures (says False).
    router = _RouterJudge(
        {
            "f1": {"refund_issued": False, "tone": 0.9},
            "f2": {"refund_issued": False, "tone": 0.9},
        }
    )
    run = run_calibration(fixtures, model_ref="x/y", llm_client=router)
    report = run.report

    # tone agrees both times; refund_issued disagrees both times.
    by_crit = {c.criterion_id: c for c in report.per_criterion}
    assert by_crit["tone"].accuracy == pytest.approx(1.0)
    assert by_crit["refund_issued"].accuracy == pytest.approx(0.0)
    dis = {d.criterion_id for d in report.disagreements}
    assert dis == {"refund_issued"}
    # The disagreement carries the judge's justification for triage.
    refund_dis = next(d for d in report.disagreements if d.criterion_id == "refund_issued")
    assert "scripted reasoning" in refund_dis.justification

    gate = decide_gate(report, threshold=0.6, metric="accuracy")
    assert gate.shippable is False


def test_calibration_errored_fixture_counts_as_failure():
    fixtures = [
        (Path("ok.yaml"), _fixture("ok", expected={"refund_issued": True, "tone": 0.9})),
        (Path("bad.yaml"), _fixture("bad", expected={"refund_issued": True, "tone": 0.9})),
    ]
    router = _RouterJudge(
        {"ok": {"refund_issued": True, "tone": 0.9}},
        error_ids={"bad"},
    )
    run = run_calibration(fixtures, model_ref="x/y", llm_client=router, max_turns=3)
    report = run.report

    assert "bad" in report.errored_fixture_ids
    # The errored fixture contributed no observations.
    assert report.total_observations == 2  # only the 'ok' fixture's two criteria
    gate = decide_gate(report, threshold=0.0, metric="accuracy")
    # Perfect agreement on the graded fixture, but the errored one fails the gate.
    assert gate.shippable is False
    assert any("errored" in r for r in gate.reasons)


class _RouterJudge:
    """One client shared across fixtures that routes verdicts by fixture id.

    ``run_calibration`` shares a single ``llm_client`` instance across fixtures,
    so this client infers which fixture is being graded from the ``[FIXTURE=...]``
    marker the test fixtures embed in their transcript (carried into the judge's
    opening user message), then emits that fixture's scripted verdict. Errored
    fixtures emit a malformed verdict so the real judge re-prompts and ERRORS.
    """

    def __init__(self, verdict_map: dict[str, dict], *, error_ids: set[str] | None = None):
        self._verdict_map = verdict_map
        self._error_ids = error_ids or set()
        self.calls = 0

    def _active_fixture(self, messages) -> str:
        opening = messages[0].content or ""
        for fid in list(self._verdict_map) + list(self._error_ids):
            if f"[FIXTURE={fid}]" in opening:
                return fid
        raise AssertionError(f"no fixture marker found in opening message: {opening!r}")

    def generate(
        self,
        system,
        messages,
        tools,
        tool_choice="auto",
        *,
        observation=None,
    ) -> GenerationResult:
        self.calls += 1
        active = self._active_fixture(messages)
        if active in self._error_ids:
            args: dict[str, object] = {"reasons": "broken"}
            for key in _submit_schema(tools):
                args[key] = "INVALID"
            return _tool_call_result(args)
        verdicts = self._verdict_map[active]
        args = {"reasons": "scripted"}
        for cid, verdict in verdicts.items():
            args[cid] = verdict
            args[f"{cid}_justification"] = f"scripted reasoning for {cid}"
        return _tool_call_result(args)
