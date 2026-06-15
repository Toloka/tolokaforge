"""Golden-trajectory grading regression test.

Pins the *current* grading verdict for a known recorded run so future changes to
``tolokaforge/core/grading/`` can prove they did not alter verdicts/scores.

This loads a recorded trajectory + final environment state + self-contained
grading config from the committed ``tau_retail_mini`` project fixture and runs it
through the existing :class:`GradingEngine`. The fixture's grading config carries a
pre-computed ``expected_state_hash``, so grading takes the deterministic
hash-comparison branch in ``combine.py`` — no LLM judge, no custom checks, no
network, and no Docker. The expected result is read from the committed golden
``grade.yaml`` rather than hard-coded, so it stays the canonical pin.
"""

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import GradingConfig, Trajectory

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

# Recorded trial fixture: trajectory + final env state + grading config + golden grade.
_PROJECT = "tau_retail_mini"
_TRIAL = Path("output/trials/test_001/0")


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def test_golden_trajectory_grading_matches_pinned_verdict(canonical_project_dir):
    """Grading the recorded trajectory reproduces the committed golden verdict."""
    trial_dir = canonical_project_dir(_PROJECT) / _TRIAL

    grading_config = GradingConfig(**_load_yaml(trial_dir / "task.yaml")["grading_config"])
    trajectory = Trajectory.model_validate(_load_yaml(trial_dir / "trajectory.yaml"))
    final_env_state = _load_yaml(trial_dir / "env.yaml")
    golden = _load_yaml(trial_dir / "grade.yaml")

    # No judge_model and no task_dir => no LLM judge and no custom checks run.
    engine = GradingEngine(grading_config=grading_config)
    grade = engine.grade_trajectory(trajectory, final_env_state)

    assert grade.binary_pass == golden["binary_pass"]
    assert grade.score == pytest.approx(golden["score"])
    assert grade.components.state_checks == pytest.approx(golden["components"]["state_checks"])
    assert grade.components.transcript_rules == golden["components"]["transcript_rules"]
    assert grade.components.llm_judge == golden["components"]["llm_judge"]
    assert grade.components.custom_checks == golden["components"]["custom_checks"]
    assert "hash matches" in grade.reasons

    # Determinism: a second grade with a fresh engine yields the identical verdict.
    grade2 = GradingEngine(grading_config=grading_config).grade_trajectory(
        trajectory, final_env_state
    )
    assert grade2.score == grade.score
    assert grade2.binary_pass == grade.binary_pass
    assert grade2.reasons == grade.reasons
