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
