"""Light unit tests for ``automation.observe`` pure helpers.

Covers the per-probe junit aggregation (``_capability_findings``) across reps and the
markdown ``render_summary`` rendering. The trajectory/wire globbing and the ``run``
I/O wrapper are exercised end-to-end by the pipeline, not here.
"""

from __future__ import annotations

import automation.observe as observe
import pytest

pytestmark = pytest.mark.unit

_REP1 = """<?xml version="1.0"?>
<testsuite>
  <testcase name="test_a"/>
  <testcase name="test_b"><failure message="boom">trace</failure></testcase>
  <testcase name="test_c"><skipped/></testcase>
</testsuite>
"""

_REP2 = """<?xml version="1.0"?>
<testsuite>
  <testcase name="test_a"/>
  <testcase name="test_b"/>
</testsuite>
"""


def test_capability_findings_aggregates_passed_and_runs_across_reps(tmp_path):
    cap = tmp_path / "capability"
    cap.mkdir()
    (cap / "u0_rep1.xml").write_text(_REP1)
    (cap / "u0_rep2.xml").write_text(_REP2)

    result = observe._capability_findings(cap, tmp_path / "__absent__.xml")

    assert result["report_present"] is True
    assert result["probes"] == 2  # test_c was skipped-only, so it is not a run
    assert result["runs_per_probe"] == 2
    assert result["probes_with_failures"] == 1
    assert result["all_passed"] is False

    by_probe = {p["probe"]: p for p in result["per_probe"]}
    assert by_probe["test_a"]["passed"] == 2 and by_probe["test_a"]["runs"] == 2
    assert by_probe["test_a"]["pass_rate"] == 1.0
    assert by_probe["test_b"]["passed"] == 1 and by_probe["test_b"]["runs"] == 2
    assert by_probe["test_b"]["pass_rate"] == 0.5
    assert by_probe["test_b"]["failure_messages"] == [{"message": "boom", "count": 1}]
    assert "test_c" not in by_probe


def test_capability_findings_absent_report(tmp_path):
    result = observe._capability_findings(tmp_path / "nope", tmp_path / "__absent__.xml")
    assert result["report_present"] is False
    assert result["all_passed"] is None
    assert result["probes"] == 0
    assert result["per_probe"] == []


def test_render_summary_shows_verdict_and_failing_probe():
    findings = {
        "candidate": {"name": "vendor/m"},
        "preset": "default",
        "capability_ran": True,
        "all_passed": False,
        "capability": {
            "report_present": True,
            "probes": 2,
            "runs_per_probe": 2,
            "probes_with_failures": 1,
            "per_probe": [
                {"probe": "test_a", "passed": 2, "runs": 2},
                {"probe": "test_b", "passed": 1, "runs": 2},
            ],
        },
        "variants": {"report_present": False},
        "wire": {
            "trials": 0,
            "tool_call_count": 0,
            "tool_arg_rejections": {
                "rejecting_trials": 0,
                "trial_rate": 0,
                "by_task_trial_rate": {},
            },
            "rejected_examples": [],
            "infra": {},
        },
    }
    summary = observe.render_summary(findings)
    assert "failures present" in summary
    assert "`vendor/m`" in summary
    assert "`test_b`: 1/2 passed" in summary


def test_render_summary_wire_only_reprobe_verdict_is_the_wire_result():
    # The final wire-verification artifact has NO capability section by design; its
    # summary must lead with the wire result, not "capability suite did NOT run -
    # infra failure" (which is the right verdict only for an observe artifact).
    findings = {
        "stage": "reprobe",
        "candidate": {"name": "vendor/m"},
        "preset": "overlay",
        "capability_ran": False,
        "all_passed": False,
        "capability": {"report_present": False},
        "variants": {"report_present": False},
        "wire": {
            "trials": 40,
            "tool_call_count": 120,
            "tool_arg_rejections": {
                "rejecting_trials": 3,
                "trial_rate": 0.075,
                "by_task_trial_rate": {},
            },
            "rejected_examples": [],
            "infra": {},
        },
    }
    summary = observe.render_summary(findings)
    assert "### Auto-integration reprobe:" in summary
    assert "wire-only pass: 3/40 trials with a tool-arg rejection" in summary
    assert "infra failure" not in summary


def _gate_findings(**overrides):
    """A clean-observe findings skeleton the gate tests mutate per case."""
    findings = {
        "capability_ran": True,
        "wire": {
            "trials": 40,
            "infra": {
                "rate_limit": 0,
                "status_error": 0,
                "max_turns": 0,
                "stuck": 0,
                "api_error": 0,
                "api_timeout": 0,
            },
        },
    }
    findings.update(overrides)
    return findings


def test_gate_clean_when_both_suites_ran_and_infra_zero():
    clean, reason = observe.evaluate_gate(_gate_findings())
    assert clean is True
    assert reason == ""


def test_gate_dirty_when_capability_did_not_run():
    clean, reason = observe.evaluate_gate(_gate_findings(capability_ran=False))
    assert clean is False
    assert "capability suite did not run" in reason


def test_gate_dirty_when_wire_never_ran():
    # The wire step is `|| true`-guarded in the workflow: a run that failed at startup
    # produces 0 trials and used to read as clean (no rejections, all-zero infra).
    clean, reason = observe.evaluate_gate(_gate_findings(wire={"trials": 0, "infra": {}}))
    assert clean is False
    assert "wire probes did not run" in reason


@pytest.mark.parametrize("key", sorted(observe.GATE_INFRA_KEYS))
def test_gate_dirty_on_each_infra_key(key):
    # api_timeout is the regression case: observe.py counted it, the old inline
    # workflow gate never read it, so an all-timeout wire run chained into resolve.
    findings = _gate_findings()
    findings["wire"]["infra"][key] = 3
    clean, reason = observe.evaluate_gate(findings)
    assert clean is False
    assert f"{key}=3" in reason


@pytest.mark.parametrize("key", ["max_turns", "stuck"])
def test_gate_ignores_model_attributable_terminations(key):
    # max_turns / stuck can be genuine model behaviour (four-bucket taxonomy) - they are
    # resolve-agent data, not contamination, and must not block the chain into resolve.
    findings = _gate_findings()
    findings["wire"]["infra"][key] = 7
    clean, _reason = observe.evaluate_gate(findings)
    assert clean is True


def test_gate_cli_prints_dirty_token_for_missing_file(tmp_path, capsys):
    assert observe.gate(str(tmp_path / "absent.json")) == 0
    out = capsys.readouterr().out
    assert out.startswith("dirty: findings unreadable")


def test_gate_cli_prints_clean_token(tmp_path, capsys):
    import json

    path = tmp_path / "findings.json"
    path.write_text(json.dumps(_gate_findings()))
    assert observe.gate(str(path)) == 0
    assert capsys.readouterr().out.strip() == "clean"
