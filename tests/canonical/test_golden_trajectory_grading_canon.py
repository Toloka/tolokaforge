"""Golden-trajectory grading regression test.

Pins the *current* grading verdict for a known bundle so future changes to
``tolokaforge/core/grading/`` can prove they did not alter verdicts/scores.

This loads a trajectory + final environment state + initial state + self-contained
grading config from the committed ``expect_initial_state_refusal`` bundle and runs it
through the existing :class:`GradingEngine`. That bundle's grading config declares
``expect_initial_state`` as its hash source, so grading takes the deterministic
hash-comparison branch in ``combine.py`` — no LLM judge, no custom checks, no
network, and no Docker. The expected result is read from the committed golden
``grade.yaml`` rather than hard-coded, so it stays the canonical pin. The bundle's
``README.md`` states plainly that it was authored rather than recorded, and derives
the pinned verdict from what it declares.

The second guard reads recorded bundles for a different property: a grading config that
serializes a ``state_checks`` key — or a ``state_checks.hash`` key — the model has since
retired still reconstructs, so re-reading a trial nobody will record again stays possible.
"""

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.state_composition import (
    RETIRED_HASH_KEYS,
    hash_block_is_a_state_source,
)
from tolokaforge.core.models import (
    RETIRED_STATE_CHECK_KEYS,
    GradingConfig,
    InitialStateConfig,
    Trajectory,
)

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

# Bundle fixture: trajectory + final env state + initial state + grading config +
# golden grade.
_BUNDLE = Path("grading_bundles/expect_initial_state_refusal")


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def test_golden_trajectory_grading_matches_pinned_verdict(test_data_dir):
    """Grading the bundle's trajectory reproduces the committed golden verdict."""
    bundle_dir = test_data_dir / _BUNDLE

    bundle = _load_yaml(bundle_dir / "bundle.yaml")
    grading_config = GradingConfig(**bundle["grading_config"])
    initial_state = InitialStateConfig(**bundle["initial_state"])
    trajectory = Trajectory.model_validate(_load_yaml(bundle_dir / "trajectory.yaml"))
    final_env_state = _load_yaml(bundle_dir / "env.yaml")
    golden = _load_yaml(bundle_dir / "grade.yaml")

    # The verdict below is a hash verdict, so a bundle edit that dropped the source
    # would otherwise re-pin a hash-free grade under the same name.
    assert hash_block_is_a_state_source(grading_config.state_checks.hash), (
        "the bundle's state_checks.hash declares nothing to compare the trial against, "
        "so this pin no longer covers the deterministic hash-comparison branch"
    )

    # No judge_model and no task_dir => no LLM judge and no custom checks run.
    engine = GradingEngine(grading_config=grading_config, task_initial_state=initial_state)
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
    assert grade.reasons == golden["reasons"]

    # Determinism: a second grade with a fresh engine yields the identical verdict.
    grade2 = GradingEngine(
        grading_config=grading_config, task_initial_state=initial_state
    ).grade_trajectory(trajectory, final_env_state)
    assert grade2.score == grade.score
    assert grade2.binary_pass == grade.binary_pass
    assert grade2.reasons == grade.reasons


def _recorded_state_checks(bundle: Path) -> dict:
    """The ``state_checks`` block a recorded bundle serialized, empty where it wrote none."""
    return (_load_yaml(bundle).get("grading_config") or {}).get("state_checks") or {}


def _recorded_hash_block(bundle: Path) -> dict:
    """The ``state_checks.hash`` block a recorded bundle serialized, empty where it wrote none."""
    return _recorded_state_checks(bundle).get("hash") or {}


_RETIRED_KEY_TIERS = (
    pytest.param(
        RETIRED_STATE_CHECK_KEYS,
        _recorded_state_checks,
        lambda config: config.state_checks,
        id="state_checks",
    ),
    pytest.param(
        frozenset(RETIRED_HASH_KEYS),
        _recorded_hash_block,
        lambda config: config.state_checks.hash,
        id="state_checks.hash",
    ),
)


@pytest.mark.parametrize(("retired", "recorded_block", "loaded_block"), _RETIRED_KEY_TIERS)
def test_every_recorded_bundle_serializing_a_retired_state_check_key_still_loads(
    test_data_dir, retired, recorded_block, loaded_block
):
    """A block refuses a key it does not declare, and these are not that.

    A recorded bundle serializes the whole grading config as the schema stood when the
    trial ran, so ``env_assertions: []`` / ``db_hash_check: false`` and a stored
    ``expected_state_hash`` are on disk in trials nobody will re-record. They are
    dropped rather than refused, which is what keeps re-reading such a bundle possible —
    and dropped rather than kept, so nothing downstream reads a key the model retired.

    The hash tier's corpus carries *populated* values, which is where this parts company
    with the tier above it: the substrate that graded those bundles never read the
    literal, so dropping it changes nothing they replay, and an author who can act meets
    the refusal at every read a pack passes through instead.

    Bundles are selected by walking the corpus rather than named, so a tier whose corpus
    is gone fails here rather than passing over nothing.
    """
    bundles = sorted(
        path for path in test_data_dir.rglob("task.yaml") if retired & set(recorded_block(path))
    )

    assert bundles, "no recorded bundle carries a retired state-check key: nothing proven"
    for bundle in bundles:
        block = loaded_block(GradingConfig(**_load_yaml(bundle)["grading_config"]))
        for key in retired:
            assert not hasattr(block, key), f"{bundle}: {key} survived the load"
