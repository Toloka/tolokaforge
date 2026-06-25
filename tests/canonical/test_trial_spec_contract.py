"""Pin the JSON wire shape of TrialSpec and TrialResult.

These are contract tests: the JSON produced by ``model_dump_json`` is the
runner gRPC payload (carried as ``RegisterTrialRequest.trial_spec_json``) and
the orchestrator-side aggregation type. Any change to the field set, the
defaults, or the embedded shapes shows up as a diff here — reviewers can
weigh whether the change is intentional.

The tests intentionally do NOT compare against an extensive snapshot of every
field of the embedded models (``TaskDescription``, ``Trajectory``, …); they
fix the new types' own shape and the round-trip invariant. Embedded models
have their own snapshot tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    Metrics,
    ModelConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.canonical


def _make_task_description() -> TaskDescription:
    return TaskDescription(
        task_id="airline_001",
        name="Book a flight",
        category="airline",
        description="The user wants to book a one-way flight from JFK to LAX.",
        adapter_type="native",
        system_prompt="You are a helpful airline assistant.",
    )


def _make_model_config() -> ModelConfig:
    return ModelConfig(name="claude-sonnet-4-6", provider="anthropic")


# ---------------------------------------------------------------------------
# TrialSpec
# ---------------------------------------------------------------------------


class TestTrialSpecContract:
    def test_required_fields_minimal_construction(self) -> None:
        spec = TrialSpec(
            trial_id="airline_001:0",
            run_id="run_2026_06_18",
            task=_make_task_description(),
            agent_model_config=_make_model_config(),
        )
        # Identity defaults: attempt_id and worker_id have sensible zero values.
        assert spec.attempt_id == 0
        assert spec.worker_id is None
        # Forward-looking extension points default to empty containers, not None.
        assert spec.env_endpoints == {}
        assert spec.runtime_context == {}
        # Embedded task is the same object (no clone, no transformation).
        assert spec.task.task_id == "airline_001"

    def test_json_round_trip_is_identity(self) -> None:
        spec = TrialSpec(
            trial_id="airline_001:3",
            run_id="run_2026_06_18",
            attempt_id=2,
            worker_id="worker-7",
            task=_make_task_description(),
            agent_model_config=_make_model_config(),
            user_model_config=_make_model_config(),
            max_turns=20,
            default_tool_timeout_s=45.0,
            env_endpoints={"db": "http://db.local:8000", "rag": "http://rag.local:8001"},
            runtime_context={"backend": "local", "pod_class": "ephemeral"},
        )
        reloaded = TrialSpec.model_validate_json(spec.model_dump_json())
        assert reloaded == spec

    def test_extra_fields_are_forbidden(self) -> None:
        # The wire format is intentionally strict — a misspelled or stale
        # field name fails validation rather than being silently dropped.
        with pytest.raises(ValidationError):
            TrialSpec.model_validate(
                {
                    "trial_id": "x:0",
                    "run_id": "r",
                    "task": _make_task_description().model_dump(),
                    "agent_model_config": _make_model_config().model_dump(),
                    "this_field_does_not_exist": True,
                }
            )

    def test_wire_shape_top_level_keys(self) -> None:
        """Lock the top-level field set so any addition or removal is reviewed."""
        spec = TrialSpec(
            trial_id="x:0",
            run_id="r",
            task=_make_task_description(),
            agent_model_config=_make_model_config(),
        )
        # The set of keys at the top level is the seam. Embedded models can
        # evolve under their own contract tests; this set may not drift
        # silently — adding a new top-level field requires updating this list.
        assert set(spec.model_dump().keys()) == {
            "trial_id",
            "run_id",
            "attempt_id",
            "worker_id",
            "task",
            "agent_model_config",
            "user_model_config",
            "max_turns",
            "default_tool_timeout_s",
            "env_endpoints",
            "runtime_context",
        }


# ---------------------------------------------------------------------------
# TrialResult
# ---------------------------------------------------------------------------


def _make_trajectory(task_id: str = "airline_001", trial_index: int = 0) -> Trajectory:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[Message(role="assistant", content="done")],
        metrics=Metrics(),
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(),
            reasons="ok",
        ),
    )


class TestTrialResultContract:
    def test_thin_wrapper_holds_trajectory_verbatim(self) -> None:
        trajectory = _make_trajectory()
        result = TrialResult.from_trajectory(trial_id="airline_001:0", trajectory=trajectory)
        # The wrapper does not copy or mutate the trajectory.
        assert result.trajectory is trajectory
        assert result.trial_id == "airline_001:0"
        assert result.worker_id is None

    def test_from_trajectory_threads_worker_id(self) -> None:
        trajectory = _make_trajectory()
        result = TrialResult.from_trajectory(
            trial_id="airline_001:0", trajectory=trajectory, worker_id="worker-7"
        )
        assert result.worker_id == "worker-7"

    def test_json_round_trip_is_identity(self) -> None:
        result = TrialResult.from_trajectory(
            trial_id="airline_001:0",
            trajectory=_make_trajectory(),
            worker_id="worker-7",
        )
        reloaded = TrialResult.model_validate_json(result.model_dump_json())
        assert reloaded == result

    def test_wire_shape_top_level_keys(self) -> None:
        """Lock the top-level field set — the trial→control surface."""
        result = TrialResult.from_trajectory(
            trial_id="airline_001:0", trajectory=_make_trajectory()
        )
        assert set(result.model_dump().keys()) == {"trial_id", "trajectory", "worker_id"}

    def test_extra_fields_are_forbidden(self) -> None:
        trajectory = _make_trajectory()
        with pytest.raises(ValidationError):
            TrialResult.model_validate(
                {
                    "trial_id": "airline_001:0",
                    "trajectory": trajectory.model_dump(mode="json"),
                    "this_field_does_not_exist": True,
                }
            )
