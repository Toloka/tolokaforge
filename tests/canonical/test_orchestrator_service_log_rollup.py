"""End-to-end lock for the producer→wire path of ``captured_service_logs``.

Drives :meth:`Orchestrator._generate_reports` against a minimal orchestrator
plus a synthetic on-disk capture tree, capturing the written aggregate via
:class:`InMemoryAggregateWriter`. Asserts the stamped
``aggregate["captured_service_logs"]`` equals the collector's own output and
validates as a :class:`CapturedServiceLogsRollup`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tolokaforge.core.compose_materialisation import run_services_dir, write_capture_manifest
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GradeComponents,
    InitialStateConfig,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
    Trajectory,
    TrialStatus,
    UserSimulatorConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.output.aggregate_models import CapturedServiceLogsRollup
from tolokaforge.core.output.aggregates import InMemoryAggregateWriter
from tolokaforge.core.output.service_log_rollup import collect_service_log_captures

pytestmark = pytest.mark.canonical


def _run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _task(task_id: str) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=f"Test {task_id}",
        category="tool_use",
        description="test",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        user_simulator=UserSimulatorConfig(mode="scripted"),
        grading="grading.yaml",
    )


def _trajectory(task_id: str, trial_index: int, *, binary_pass: bool) -> Trajectory:
    now = datetime.now(tz=UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED if binary_pass else TrialStatus.FAILED,
        messages=[],
        metrics=Metrics(latency_total_s=5.0, turns=10, tool_calls=5, cost_usd=0.01),
        grade=Grade(
            binary_pass=binary_pass,
            score=1.0 if binary_pass else 0.0,
            components=GradeComponents(state_checks=1.0 if binary_pass else 0.0),
        ),
    )


def test_generate_reports_stamps_collector_output(tmp_path: Path) -> None:
    """``_generate_reports`` writes ``captured_service_logs`` equal to the
    collector's roll-up over the on-disk tree, and the stamped sub-object
    is a valid ``CapturedServiceLogsRollup``."""
    # Synthetic capture tree: one provision-fail bundle + one run-level bundle.
    prov = tmp_path / "trials" / "T1" / "0" / "services"
    prov.mkdir(parents=True)
    write_capture_manifest(prov, tail=500, captured={"db": 2048}, capture_reason="provision_error")
    shared = run_services_dir(tmp_path)
    shared.mkdir(parents=True)
    write_capture_manifest(shared, tail=500, captured={"api": 1024})

    writer = InMemoryAggregateWriter()
    orch = Orchestrator(_run_config(), deps=OrchestratorDeps(run_aggregate_writer=writer))
    orch.tasks = [_task("T1"), _task("T2")]
    orch.results = [
        _trajectory("T1", 0, binary_pass=False),
        _trajectory("T2", 0, binary_pass=True),
    ]

    orch._generate_reports(tmp_path)

    aggregate = writer.runs[tmp_path].aggregate
    assert aggregate is not None
    expected = collect_service_log_captures(tmp_path).model_dump(by_alias=True, mode="json")
    assert aggregate["captured_service_logs"] == expected
    assert aggregate["captured_service_logs"]["captures"] == 2

    # The stamped sub-object is a valid roll-up.
    CapturedServiceLogsRollup.model_validate(aggregate["captured_service_logs"])


def test_generate_reports_clean_run_emits_zero_envelope(tmp_path: Path) -> None:
    """A run with no captures still stamps the explicit zero roll-up, so the
    key is present and distinguishable from a pre-feature aggregate."""
    writer = InMemoryAggregateWriter()
    orch = Orchestrator(_run_config(), deps=OrchestratorDeps(run_aggregate_writer=writer))
    orch.tasks = [_task("T1")]
    orch.results = [_trajectory("T1", 0, binary_pass=True)]

    orch._generate_reports(tmp_path)

    aggregate = writer.runs[tmp_path].aggregate
    assert aggregate is not None
    assert aggregate["captured_service_logs"] == {
        "captures": 0,
        "total_bytes": 0,
        "per_service_bytes": {},
        "entries": [],
    }
    CapturedServiceLogsRollup.model_validate(aggregate["captured_service_logs"])
