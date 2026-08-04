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

The second guard reads recorded bundles for a different property: every grading config
the tree has on disk still reconstructs, including the four that serialize a
``state_checks`` key the model has since retired.
"""

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.models import RETIRED_STATE_CHECK_KEYS, GradingConfig, Trajectory

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
    # Every registered component, so a new one cannot be silently unasserted here.
    # A key the golden grade.yaml never wrote means the component went unevaluated.
    for spec in GRADE_COMPONENTS:
        pinned = golden["components"].get(spec.name)
        scored = getattr(grade.components, spec.core_field)
        expected = pinned if pinned is None else pytest.approx(pinned)
        message = f"{spec.name}: golden pins {pinned!r}, grading produced {scored!r}"
        assert scored == expected, message
    assert "hash matches" in grade.reasons

    # Determinism: a second grade with a fresh engine yields the identical verdict.
    grade2 = GradingEngine(grading_config=grading_config).grade_trajectory(
        trajectory, final_env_state
    )
    assert grade2.score == grade.score
    assert grade2.binary_pass == grade.binary_pass
    assert grade2.reasons == grade.reasons


# How many recorded bundles in the tree serialize a retired state-check key. Pinned so
# a corpus that stops carrying them is a visible change to what this guard proves,
# rather than a walk over nothing that passes.
_BUNDLES_WITH_A_RETIRED_STATE_CHECK_KEY = 4


def _recorded_state_checks(bundle: Path) -> dict:
    """The ``state_checks`` block a recorded bundle serialized, empty where it wrote none."""
    return (_load_yaml(bundle).get("grading_config") or {}).get("state_checks") or {}


def test_every_recorded_bundle_serializing_a_retired_state_check_key_still_loads(test_data_dir):
    """``state_checks`` refuses a key it does not declare, and these are not that.

    A recorded bundle serializes the whole grading config as the schema stood when the
    trial ran, so ``env_assertions: []`` / ``db_hash_check: false`` are on disk in
    trials nobody will re-record. They are dropped rather than refused, which is what
    keeps re-reading such a bundle possible — and dropped rather than kept, so nothing
    downstream reads a key the model retired.
    """
    bundles = sorted(
        path
        for path in test_data_dir.rglob("task.yaml")
        if RETIRED_STATE_CHECK_KEYS & set(_recorded_state_checks(path))
    )

    assert len(bundles) == _BUNDLES_WITH_A_RETIRED_STATE_CHECK_KEY, bundles
    for bundle in bundles:
        state_checks = GradingConfig(**_load_yaml(bundle)["grading_config"]).state_checks
        for key in RETIRED_STATE_CHECK_KEYS:
            assert not hasattr(state_checks, key), f"{bundle}: {key} survived the load"
