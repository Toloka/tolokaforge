"""Real-task canonical lock: the adapter-declared trial-grader name reaches the grade.

Drives a genuine :meth:`Orchestrator.run` over the bundled native ``tool_use``
pack with a :class:`NativeAdapter` subclass declaring a custom
``trial_grader_name``. A recording grader registered under that name stamps a
unique sentinel into the :class:`~tolokaforge.core.models.Grade`; a minimal
recording conductor routes grading through ``ctx.trial_grader``. The sentinel on
the collected grade proves the adapter-declared name flowed orchestrator
(``_build_conductor``) → ``ConductorContext.trial_grader`` → the conductor's
grade call — the positive custom-grader path end-to-end.

Hermetic — no services, no LLM, no Docker — so it runs on every PR. The
equivalent real-runner parity lock in ``tests/integration/test_run_trial_e2e.py``
is gated to the push/nightly/gate lane and would hide a regression in this seam,
so the fast composition lock lives in the canonical tier.
"""

from __future__ import annotations

import importlib.metadata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import plugin_registry
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.plugin_registry import TRIAL_GRADERS_GROUP
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = _REPO_ROOT / "examples" / "native" / "tool_use" / "dataset"
_TASK_ID = "tool_use_public_example_01"
_RECORDING_GRADER_NAME = "recording_grader_e2e"

#: Sentinel Grade values no built-in grader produces — asserting these proves the
#: grade came from the recording grader the adapter's name selected.
_SENTINEL_SCORE = 0.4242
_SENTINEL_REASON = "adapter-declared-grader-name sentinel"

_FIXED_TS = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _RecordingGrader:
    """``TrialGrader`` returning a fixed sentinel :class:`Grade`."""

    def grade(self, spec: TrialSpec, trajectory: Trajectory, agent_system_prompt: str) -> Grade:
        return Grade(binary_pass=True, score=_SENTINEL_SCORE, reasons=_SENTINEL_REASON)


class _RecordingConductor:
    """Hermetic ``Conductor`` that grades through the injected ``ctx.trial_grader``.

    Emits a synthetic trajectory (no LLM, no RPC) and stamps the grade the
    orchestrator-resolved grader produces onto it — so the resulting grade
    exercises the by-name grader seam rather than a side channel.
    """

    def __init__(self, grader: _RecordingGrader) -> None:
        self._grader = grader

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        task_id, trial_idx = spec.trial_id.rsplit(":", 1)
        trajectory = Trajectory(
            task_id=task_id,
            trial_index=int(trial_idx),
            start_ts=_FIXED_TS,
            end_ts=_FIXED_TS,
            status=TrialStatus.COMPLETED,
            messages=[],
        )
        trajectory.grade = self._grader.grade(spec, trajectory, agent_system_prompt="")
        return TrialResult.from_trajectory(
            trial_id=spec.trial_id, trajectory=trajectory, worker_id=None
        )


class _RecordingGraderAdapter(NativeAdapter):
    trial_grader_name = _RECORDING_GRADER_NAME


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(self, name: str, factory: Any) -> None:
        self.name = name
        self.dist = _FakeDist("recording-grader-e2e")
        self._factory = factory

    def load(self) -> Any:
        return self._factory


@pytest.fixture
def _only_recording_grader(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Register ONLY the recording grader under the trial-graders group.

    Non-grader groups delegate to real discovery (the adapter registry resolves
    ``native`` through it), so the sole by-name resolution the test controls is
    the grader seam.
    """
    real_entry_points = importlib.metadata.entry_points

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == TRIAL_GRADERS_GROUP:
            return [_FakeEntryPoint(_RECORDING_GRADER_NAME, lambda _ctx: _RecordingGrader())]
        return list(real_entry_points(group=group))

    plugin_registry._clear_discovery_cache()
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    yield
    plugin_registry._clear_discovery_cache()


def test_adapter_declared_grader_name_reaches_grade(
    _only_recording_grader: None, tmp_path: Path
) -> None:
    """The custom grader the adapter names produces the grade of a real run."""
    # Sanity: the only registered grader is the recording one — no built-in leaks in.
    assert plugin_registry.available_trial_graders() == [_RECORDING_GRADER_NAME]

    adapter = _RecordingGraderAdapter({"base_dir": str(_PACK), "tasks_glob": "tasks/**/task.yaml"})
    task = adapter.get_task(_TASK_ID)

    config = RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results")),
    )
    captured: dict[str, Any] = {}

    def conductor_factory(ctx: Any) -> _RecordingConductor:
        captured["grader"] = ctx.trial_grader
        return _RecordingConductor(ctx.trial_grader)

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=conductor_factory,
        ),
    )
    orch.tasks = [task]
    orch.adapter = adapter
    orch.run()

    # The orchestrator resolved the grader by the adapter-declared name and
    # placed it into ctx.trial_grader before the conductor factory ran, so the
    # captured grader is the one the name selected.
    assert isinstance(captured["grader"], _RecordingGrader)

    (trajectory,) = orch.results
    assert trajectory.grade is not None
    assert trajectory.grade.score == _SENTINEL_SCORE
    assert trajectory.grade.reasons == _SENTINEL_REASON
