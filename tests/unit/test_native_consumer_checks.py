"""Regression tests for the native consumer-surface contract (the D16 class).

A native ``grading.yaml`` has three real consumers — the adapter load path,
the core ``GradingConfig`` parse, and the full runner translation — and a
positively weighted grading component can silently vanish in translation.
``native-verify`` must catch all of that at author time, in-band, citing the
exact surface that rejected the content.

The ``vetchain_ops_seed10`` fixture is the REAL seed-10 workspace that died
live on 2026-07-17 (bg4t4d0jg): ``testcases/sent_mut_001/grading.yaml``
authored ``state_checks`` with golden actions but no ``combine`` and no
``enabled: true``; it passed golden replay and then killed the run at trial
registration with zero diagnosis.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tolokaforge.adapters.native_consumer_checks import check_consumer_surfaces
from tolokaforge.adapters.native_verify import verify_native_tasks

pytestmark = pytest.mark.unit

SEED10_DOMAIN = Path(__file__).parents[1] / "fixtures" / "native" / "vetchain_ops_seed10"
NOTES_DOMAIN = (
    Path(__file__).parents[2] / "examples" / "native" / "native_shared_domain" / "dataset" / "notes"
)

CONSUMER_CHECKS = (
    "consumer_adapter_load",
    "consumer_core_grading_config",
    "consumer_runner_translation",
    "grading_component_survival",
)


def _check(case, name):
    matches = [check for check in case.checks if check.name == name]
    assert matches, f"check {name!r} missing from {[c.name for c in case.checks]}"
    return matches[0]


def test_d16_seed10_grading_fails_citing_the_rejecting_surface() -> None:
    """The exact seed-10 sent_mut_001 tree must now FAIL native-verify."""
    report = verify_native_tasks(str(SEED10_DOMAIN / "testcases" / "*" / "task.yaml"))

    assert not report.passed
    (case,) = report.cases
    assert case.task_id == "sent_mut_001"

    # The old blind spot: machinery replay still passes.
    assert _check(case, "golden_replay").passed

    # All three surfaces were exercised; the report cites WHICH one rejected.
    assert _check(case, "consumer_adapter_load").passed
    core = _check(case, "consumer_core_grading_config")
    assert not core.passed
    assert "combine" in core.detail
    assert _check(case, "consumer_runner_translation").passed

    # Active-weight survival: the only weighted component evaluates to nothing
    # because ``hash`` was authored without ``enabled: true``.
    survival = _check(case, "grading_component_survival")
    assert not survival.passed
    (component,) = case.grading_components
    assert component.component == "state_checks"
    assert component.present and not component.evaluable and not component.survived
    assert "enabled" in component.reason


def test_weighted_llm_judge_without_model_ref_is_a_dropped_component(
    tmp_path: Path,
) -> None:
    """core allows model_ref=None, the runner requires it, the adapter omits
    the judge — the silent drop must surface as a dropped-component finding."""
    domain = tmp_path / "notes"
    shutil.copytree(NOTES_DOMAIN, domain)
    grading = domain / "testcases" / "add_first_note" / "grading.yaml"
    grading.write_text(
        """
combine:
  method: weighted
  weights:
    transcript_rules: 0.5
    llm_judge: 0.5
  pass_threshold: 0.75

transcript_rules:
  max_turns: 6
  required_actions:
    - action_id: "save_note"
      requestor: assistant
      name: add_note
      arguments: {}
      compare_args: []

llm_judge:
  rubric: "Did the agent save the note politely?"
  output_schema: {"type": "object"}
""",
        encoding="utf-8",
    )

    report = verify_native_tasks(str(domain / "testcases" / "add_first_note" / "task.yaml"))

    assert not report.passed
    (case,) = report.cases
    # Every surface individually accepts the content — that is the trap.
    assert _check(case, "consumer_adapter_load").passed
    assert _check(case, "consumer_core_grading_config").passed
    assert _check(case, "consumer_runner_translation").passed

    survival = _check(case, "grading_component_survival")
    assert not survival.passed
    fates = {component.component: component for component in case.grading_components}
    assert fates["transcript_rules"].survived
    judge = fates["llm_judge"]
    assert not judge.present and not judge.survived
    assert "model_ref" in judge.reason
    assert judge.weight == 0.5
    assert judge.declared_in == "source_combine"


def test_fully_valid_case_passes_all_consumer_surfaces() -> None:
    report = verify_native_tasks(str(NOTES_DOMAIN / "testcases" / "*" / "task.yaml"))

    assert report.passed
    assert len(report.cases) == 2
    for case in report.cases:
        for name in CONSUMER_CHECKS:
            assert _check(case, name).passed, name
        assert all(component.survived for component in case.grading_components)


def test_check_consumer_surfaces_never_raises_on_garbage(tmp_path: Path) -> None:
    """Converge-shaped: internal failures fold into findings, never raise."""
    missing = tmp_path / "nowhere" / "task.yaml"
    report = check_consumer_surfaces(missing)
    assert not report.passed
    surfaces = {finding.surface: finding for finding in report.surfaces}
    assert set(surfaces) == {"adapter_load", "core_grading_config", "runner_translation"}
    assert not surfaces["adapter_load"].passed

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "task.yaml").write_text("task_id: [unclosed", encoding="utf-8")
    report = check_consumer_surfaces(broken / "task.yaml")
    assert not report.passed
