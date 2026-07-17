"""Real-LLM calibration over the committed golden fixture (gated on a key).

Runs the full calibration pipeline — real ``LLMJudge`` over the bundled
golden fixture with a cheap model — and asserts it completes and produces a
report. It does NOT assert an exact agreement number: the judge is agentic and
nondeterministic even at temperature 0 (Stage-4 design note), so pinning κ would
be flaky. The deterministic agreement maths is pinned by ``test_metrics.py`` and
the plumbing by ``test_harness_scripted.py``.

Run with:
    scripts/with_env.sh uv run pytest \\
        tools/rubric-calibrator/tests/test_calibration_live.py -q -m integration
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rubric_calibrator.fixture import load_fixtures
from rubric_calibrator.metrics import decide_gate
from rubric_calibrator.runner import run_calibration

pytestmark = [pytest.mark.integration, pytest.mark.requires_api, pytest.mark.llm]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _pick_model() -> str | None:
    """Pick a cheap model whose key is resolvable via SecretManager.

    Routed through ``SecretManager`` (not raw ``os.environ``) to honour the
    repo's "no raw os.environ for keys" rule. The ``requires_api`` auto-skip in
    conftest.py guarantees this only runs when a key is actually present.
    """
    from tolokaforge.secrets import get_default

    secrets = get_default()
    if secrets.get_secret("OPENROUTER_API_KEY"):
        return "openrouter/openai/gpt-4.1-mini"
    if secrets.get_secret("OPENAI_API_KEY"):
        return "openai/gpt-4.1-mini"
    return None


def test_calibration_over_golden_fixture_completes():
    model_ref = _pick_model()
    if model_ref is None:
        pytest.skip("No OPENROUTER_API_KEY / OPENAI_API_KEY set")
    loaded = load_fixtures([FIXTURES_DIR])
    run = run_calibration(loaded, model_ref=model_ref, max_turns=10, episode_timeout_s=180)

    report = run.report
    # Completed without error and produced one observation per criterion.
    assert not report.has_errors, [o.judge_result.reasons for o in run.outcomes if o.errored]
    assert report.total_observations == 4
    assert {c.criterion_id for c in report.per_criterion} == {
        "refund_issued",
        "amount_quoted",
        "offered_credit_first",
        "tone",
    }
    # A report and a gate decision are produced (we don't pin the κ value).
    gate = decide_gate(report, threshold=0.6, metric="kappa")
    assert isinstance(gate.shippable, bool)
    # Judge usage was recorded.
    assert run.total_usage.calls >= 1
    assert run.total_usage.prompt_tokens > 0
