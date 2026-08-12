"""Unit tests for output writer module"""

import pytest
import yaml

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    Metrics,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output_writer import TRIAL_BUNDLE_SCHEMA_VERSION, OutputWriter

pytestmark = pytest.mark.unit


@pytest.fixture
def sample_trajectory():
    """Create a sample trajectory for testing"""
    from datetime import datetime

    messages = [
        Message(role=MessageRole.USER, content="Hello"),
        Message(role=MessageRole.ASSISTANT, content="Hi! How can I help?"),
    ]

    return Trajectory(
        task_id="test-task-123",
        trial_index=0,
        start_ts=datetime(2025, 1, 1, 10, 0, 0),
        end_ts=datetime(2025, 1, 1, 10, 5, 0),
        status=TrialStatus.COMPLETED,
        messages=messages,
        metrics=Metrics(latency_total_s=300.0, turns=2, tool_calls=3),
        tool_log=[
            recorded_call("get_user", sequence=0, latency_seconds=0.25),
            recorded_call("create_order", sequence=1, latency_seconds=1.5),
            recorded_call(
                "create_order",
                sequence=2,
                status=ToolExecutionStatus.ERROR,
                latency_seconds=0.75,
            ),
        ],
    )


@pytest.fixture
def sample_grade():
    """Create a sample grade for testing"""
    return Grade(
        binary_pass=True,
        score=0.95,
        components=GradeComponents(state_checks=1.0, transcript_rules=0.9),
        reasons="All checks passed",
        state_diff={"has_diff": False},
    )


def test_write_trajectory(tmp_path, sample_trajectory):
    """Test writing trajectory.yaml"""
    writer = OutputWriter(tmp_path)

    writer.write_trajectory(sample_trajectory)

    traj_file = tmp_path / "trajectory.yaml"
    assert traj_file.exists()

    with open(traj_file) as f:
        data = yaml.safe_load(f)

    assert data["task_id"] == "test-task-123"
    assert data["trial_index"] == 0
    assert data["status"] == "completed"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][1]["role"] == "assistant"


def test_write_tool_log(tmp_path, sample_trajectory):
    """tool_log.yaml carries every recorded field, in ``sequence`` order.

    The record is the grader's view of the trial. Three of its fields — ``executor``,
    ``latency_seconds`` and ``sequence`` — have no conversational representation at
    all, so nothing but this file can carry them into a re-grade.
    """
    writer = OutputWriter(tmp_path)

    writer.write_tool_log(sample_trajectory)

    with open(tmp_path / "tool_log.yaml") as f:
        record = yaml.safe_load(f)

    assert [call["sequence"] for call in record] == [0, 1, 2]
    assert [call["tool_name"] for call in record] == ["get_user", "create_order", "create_order"]
    assert [call["status"] for call in record] == ["success", "success", "error"]
    assert [call["executor"] for call in record] == ["agent", "agent", "agent"]
    assert [call["latency_seconds"] for call in record] == [0.25, 1.5, 0.75]
    assert {call["call_id"] for call in record} == {
        call.call_id for call in sample_trajectory.tool_log
    }


def test_write_tool_log_writes_an_empty_record_for_a_trial_that_called_no_tool(
    tmp_path, sample_trajectory
):
    """The file is written empty rather than skipped.

    An absent ``tool_log.yaml`` means the bundle carries no record at all, which is
    what a bundle written before the artifact existed looks like. A trial that simply
    never called a tool is a different fact about the trial, and the reader is
    entitled to tell them apart.
    """
    writer = OutputWriter(tmp_path)
    sample_trajectory.tool_log = []

    writer.write_tool_log(sample_trajectory)

    tool_log_file = tmp_path / "tool_log.yaml"
    assert tool_log_file.exists()
    with open(tool_log_file) as f:
        assert yaml.safe_load(f) == []


def test_write_metrics(tmp_path, sample_trajectory):
    """Test writing metrics.yaml with tool usage breakdown"""
    writer = OutputWriter(tmp_path)

    writer.write_metrics(sample_trajectory)

    metrics_file = tmp_path / "metrics.yaml"
    assert metrics_file.exists()

    with open(metrics_file) as f:
        data = yaml.safe_load(f)

    assert data["latency_total_s"] == 300.0
    assert data["turns"] == 2
    assert data["tool_calls"] == 3
    assert data["schema_version"] == TRIAL_BUNDLE_SCHEMA_VERSION

    assert "tool_usage" in data
    tool_usage = data["tool_usage"]
    assert len(tool_usage) == 2

    create_order_stats = next(t for t in tool_usage if t["tool_name"] == "create_order")
    assert create_order_stats["call_count"] == 2
    assert create_order_stats["success_count"] == 1
    assert create_order_stats["error_count"] == 1
    # Summed from each recorded call's measured wall time, failures included.
    assert create_order_stats["total_duration_s"] == pytest.approx(2.25)


def test_write_grade(tmp_path, sample_grade):
    """Test writing grade.yaml"""
    writer = OutputWriter(tmp_path)

    writer.write_grade(sample_grade)

    grade_file = tmp_path / "grade.yaml"
    assert grade_file.exists()

    with open(grade_file) as f:
        data = yaml.safe_load(f)

    assert data["binary_pass"] is True
    assert data["score"] == 0.95
    assert data["components"]["state_checks"] == 1.0
    assert data["state_diff"]["has_diff"] is False


def test_write_all(tmp_path, sample_trajectory, sample_grade):
    """Test writing all files at once"""
    writer = OutputWriter(tmp_path)

    task_config = {
        "task_id": "test-task-123",
        "trial_index": 0,
        "category": "test",
        "description": "Test",
        "grading_config": {},
        "tools": {},
        "policies": {},
    }

    env_state = {"db": {}}

    logger = StructuredLogger("test_trial")
    logger.info("Test log")

    sample_trajectory.grade = sample_grade

    writer.write_all(sample_trajectory, task_config, env_state, logger)

    assert (tmp_path / "task.yaml").exists()
    assert (tmp_path / "trajectory.yaml").exists()
    assert (tmp_path / "tool_log.yaml").exists()
    assert (tmp_path / "env.yaml").exists()
    assert (tmp_path / "metrics.yaml").exists()
    assert (tmp_path / "grade.yaml").exists()
    assert (tmp_path / "logs.yaml").exists()


def test_write_all_without_grade(tmp_path, sample_trajectory):
    """Test write_all when grade is None"""
    writer = OutputWriter(tmp_path)

    task_config = {
        "task_id": "test-task-123",
        "trial_index": 0,
        "category": "test",
        "description": "Test",
        "grading_config": {},
        "tools": {},
        "policies": {},
    }

    env_state = {"db": {}}
    logger = StructuredLogger("test_trial")

    writer.write_all(sample_trajectory, task_config, env_state, logger)

    assert (tmp_path / "task.yaml").exists()
    assert (tmp_path / "trajectory.yaml").exists()
    assert (tmp_path / "tool_log.yaml").exists()
    assert (tmp_path / "env.yaml").exists()
    assert (tmp_path / "metrics.yaml").exists()
    assert (tmp_path / "logs.yaml").exists()
    assert not (tmp_path / "grade.yaml").exists()


def test_write_trajectory_does_not_include_system_prompt(tmp_path):
    """write_trajectory() must NOT include the agent system prompt — that
    moved to ``prompts.yaml`` (written separately by
    :class:`FileArtifactWriter.write_prompts`) so the message trace
    stays lean."""
    from datetime import datetime

    trajectory = Trajectory(
        task_id="test-sys-prompt",
        trial_index=0,
        start_ts=datetime(2025, 1, 1, 10, 0, 0),
        end_ts=datetime(2025, 1, 1, 10, 5, 0),
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="Hello"),
        ],
    )

    writer = OutputWriter(tmp_path)
    writer.write_trajectory(trajectory)

    with open(tmp_path / "trajectory.yaml") as f:
        data = yaml.safe_load(f)

    assert "system_prompt" not in data
    assert "user_system_prompt" not in data
    # ``simulator_schema_version`` stays — it's metadata about the
    # message-trace shape, not a prompt itself.
    assert data["simulator_schema_version"] == 2


def test_write_task_info_writes_every_key_the_caller_supplied(tmp_path):
    """The snapshot is the definition of task.yaml's shape: keys the writer has
    no knowledge of reach the file, in the caller's order."""
    writer = OutputWriter(tmp_path)

    task_config = {
        "task_id": "test-full-snapshot",
        "trial_index": 0,
        "category": "test",
        "description": "Test",
        "interaction_mode": "conversational",
        "initial_user_message": "Hi, I need to replace my pass.",
        "user_actor": {"mode": "llm", "persona": "frustrated commuter"},
        "grading_config": {},
        "tools": {},
        "policies": {},
        "model_config": {
            "agent": {"provider": "openai", "name": "gpt-4", "temperature": 0.0},
            "user": None,
        },
    }

    writer.write_task_info(task_config)

    with open(tmp_path / "task.yaml") as f:
        data = yaml.safe_load(f)

    assert data == task_config
    assert list(data) == list(task_config)


def test_write_task_info_invents_no_key_the_caller_omitted(tmp_path):
    """A partial snapshot writes a partial file — the writer supplies no
    defaults of its own for keys the caller left out."""
    writer = OutputWriter(tmp_path)

    task_config = {"task_id": "test-partial", "description": "Test"}

    writer.write_task_info(task_config)

    with open(tmp_path / "task.yaml") as f:
        data = yaml.safe_load(f)

    assert data == task_config
