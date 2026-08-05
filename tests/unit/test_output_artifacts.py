"""``tolokaforge.core.output.artifacts`` unit tests.

Covers:

* :func:`model_id_slug` — deterministic, filesystem-safe slug over every
  real-world model name registered in
  [`model_presets.yaml`](../../tolokaforge/core/data/model_presets.yaml).
* :class:`FileArtifactWriter.write_tools_schemas` — per-trial YAML
  artifact at ``trial_dir/tools_schemas.yaml``. Latest write wins
  (the trial dir is recreated fresh by the orchestrator).
* :func:`read_recorded_tool_log` — reads ``tool_log.yaml`` back, keeping
  a bundle that carries no record apart from a trial that called no tool.
* Protocol conformance — :class:`FileArtifactWriter` satisfies the
  :class:`TrialArtifactWriter` structural type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    TrialArtifactWriter,
    model_id_slug,
    read_recorded_tool_log,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# model_id_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "name", "expected"),
    [
        ("openrouter", "anthropic/claude-opus-4.7", "openrouter__anthropic_claude-opus-4.7"),
        ("openai", "gpt-5.5", "openai__gpt-5.5"),
        ("openai", "openai/gpt-5.5", "openai__openai_gpt-5.5"),
        ("nova", "amazon-nova-micro-1_0", "nova__amazon-nova-micro-1_0"),
        ("xai", "x-ai/grok-4", "xai__x-ai_grok-4"),
        ("qwen", "qwen/qwen3-next", "qwen__qwen_qwen3-next"),
        (
            "anthropic",
            "anthropic/claude-sonnet-4.7-20260101",
            "anthropic__anthropic_claude-sonnet-4.7-20260101",
        ),
        # Unsafe separators + runs collapse cleanly.
        ("provider with space", "model:v1 @ latest", "provider_with_space__model_v1_latest"),
        # Missing provider / name fall back to placeholders.
        ("", "only-name", "unknown__only-name"),
        ("only-provider", "", "only-provider__unnamed"),
    ],
)
def test_model_id_slug_parametrised(provider: str, name: str, expected: str) -> None:
    assert model_id_slug(provider, name) == expected


def test_model_id_slug_stable_across_calls() -> None:
    """Slugs must be a pure function of input — no hidden state / randomness."""
    a = model_id_slug("openai", "gpt-5.5")
    b = model_id_slug("openai", "gpt-5.5")
    assert a == b


def test_model_id_slug_filesystem_safe() -> None:
    """Every byte of the slug must be in the safe-name character class."""
    slug = model_id_slug("openrouter/foo", "anthropic/claude-opus-4.7")
    assert all(ch.isalnum() or ch in "._-" for ch in slug), slug


# ---------------------------------------------------------------------------
# FileArtifactWriter.write_tools_schemas — per-trial YAML
# ---------------------------------------------------------------------------


def test_write_tools_schemas_creates_yaml_in_trial_dir(tmp_path: Path) -> None:
    """The post-policy schema lands at ``trial_dir/tools_schemas.yaml`` and
    round-trips through the YAML loader byte-for-byte."""
    writer = FileArtifactWriter()
    schemas = [{"type": "function", "function": {"name": "foo", "parameters": {}}}]
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_tools_schemas(trial_dir, schemas)

    target = trial_dir / "tools_schemas.yaml"
    assert target.exists()
    assert yaml.safe_load(target.read_text()) == schemas


def test_write_tools_schemas_creates_trial_dir_when_missing(tmp_path: Path) -> None:
    """The writer creates the trial directory if the orchestrator hasn't
    yet — defensive: callers that hand a fresh path must not crash."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "fresh" / "trial"
    assert not trial_dir.exists()

    writer.write_tools_schemas(trial_dir, [{"name": "x"}])

    assert (trial_dir / "tools_schemas.yaml").exists()


def test_write_tools_schemas_overwrites_on_repeat_write(tmp_path: Path) -> None:
    """No filename dedup — every call writes. The orchestrator owns the
    trial dir and only writes once per trial; if anyone *does* write
    twice (e.g. a re-run on the same path), the latest payload wins."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"
    first_payload = [{"name": "original"}]
    second_payload = [{"name": "rewrite"}]

    writer.write_tools_schemas(trial_dir, first_payload)
    writer.write_tools_schemas(trial_dir, second_payload)

    target = trial_dir / "tools_schemas.yaml"
    assert yaml.safe_load(target.read_text()) == second_payload


def test_write_tools_schemas_distinct_trials_distinct_files(tmp_path: Path) -> None:
    """Each (task, trial_index) pair writes its own file — no cross-trial
    sharing, every bundle is self-contained."""
    writer = FileArtifactWriter()
    schemas_a = [{"name": "for_task_a"}]
    schemas_b = [{"name": "for_task_b"}]

    writer.write_tools_schemas(tmp_path / "trials" / "task_a" / "0", schemas_a)
    writer.write_tools_schemas(tmp_path / "trials" / "task_a" / "1", schemas_a)
    writer.write_tools_schemas(tmp_path / "trials" / "task_b" / "0", schemas_b)

    a0 = tmp_path / "trials" / "task_a" / "0" / "tools_schemas.yaml"
    a1 = tmp_path / "trials" / "task_a" / "1" / "tools_schemas.yaml"
    b0 = tmp_path / "trials" / "task_b" / "0" / "tools_schemas.yaml"
    assert a0.exists() and a1.exists() and b0.exists()
    assert yaml.safe_load(a0.read_text()) == schemas_a
    assert yaml.safe_load(b0.read_text()) == schemas_b


def test_write_tools_schemas_lives_alongside_other_trial_artifacts(
    tmp_path: Path,
) -> None:
    """Smoke check that ``tools_schemas.yaml`` lands in the same directory
    as the other six trial artifacts, not in a separate sidecar tree."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_tools_schemas(trial_dir, [{"name": "tool"}])

    assert (trial_dir / "tools_schemas.yaml").exists()
    # No legacy sidecar tree at the results-root level.
    assert not (tmp_path / "tools_schemas").exists()


# ---------------------------------------------------------------------------
# FileArtifactWriter.write_prompts — per-trial YAML
# ---------------------------------------------------------------------------


def test_write_prompts_creates_yaml_in_trial_dir(tmp_path: Path) -> None:
    """``prompts.yaml`` lands inside the trial bundle, alongside
    ``trajectory.yaml``. Both prompt strings are persisted under the
    same field names they used to occupy on ``Trajectory`` so external
    tooling that knows ``system_prompt`` / ``user_system_prompt`` keeps
    working."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_prompts(
        trial_dir,
        agent_prompt="You are an agent.",
        user_prompt="You are a user simulator.",
    )

    target = trial_dir / "prompts.yaml"
    assert target.exists()
    assert yaml.safe_load(target.read_text()) == {
        "system_prompt": "You are an agent.",
        "user_system_prompt": "You are a user simulator.",
    }


def test_write_prompts_handles_none(tmp_path: Path) -> None:
    """Both prompts may be ``None`` — for non-LLM agents (no system
    prompt) and scripted user simulators (no LLM-shaped user prompt).
    Persist explicit ``None`` so consumers can distinguish *absent* from
    *missing field*."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_prompts(trial_dir, agent_prompt=None, user_prompt=None)

    target = trial_dir / "prompts.yaml"
    assert target.exists()
    assert yaml.safe_load(target.read_text()) == {
        "system_prompt": None,
        "user_system_prompt": None,
    }


def test_write_prompts_creates_trial_dir_when_missing(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "fresh" / "trial"
    assert not trial_dir.exists()

    writer.write_prompts(trial_dir, agent_prompt="A", user_prompt="B")

    assert (trial_dir / "prompts.yaml").exists()


def test_write_prompts_overwrites_on_repeat_write(tmp_path: Path) -> None:
    """Same orchestrator-owns-trial-dir invariant as ``write_tools_schemas``:
    every call writes; the latest payload wins."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_prompts(trial_dir, agent_prompt="first", user_prompt="u1")
    writer.write_prompts(trial_dir, agent_prompt="second", user_prompt="u2")

    target = trial_dir / "prompts.yaml"
    data = yaml.safe_load(target.read_text())
    assert data == {"system_prompt": "second", "user_system_prompt": "u2"}


def test_write_prompts_lives_alongside_other_trial_artifacts(tmp_path: Path) -> None:
    """``prompts.yaml`` is part of the trial bundle, not a sidecar tree."""
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"

    writer.write_prompts(trial_dir, agent_prompt="A", user_prompt="B")

    assert (trial_dir / "prompts.yaml").exists()
    assert not (tmp_path / "prompts").exists()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_file_artifact_writer_satisfies_protocol() -> None:
    """Structural check: a :class:`FileArtifactWriter` instance is accepted
    where a :class:`TrialArtifactWriter` is expected."""
    writer: TrialArtifactWriter = FileArtifactWriter()
    assert isinstance(writer, FileArtifactWriter)


# ---------------------------------------------------------------------------
# Per-trial delegation
# ---------------------------------------------------------------------------


def _sample_trajectory() -> Trajectory:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trajectory(
        task_id="artifacts-test",
        trial_index=0,
        system_prompt="sys",
        user_system_prompt="user-sim-sys",
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.COMPLETED,
        messages=[Message(role=MessageRole.USER, content="hi", ts=ts)],
    )


def test_file_artifact_writer_per_trial_delegates(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    trial_dir = tmp_path / "trials" / "task_a" / "0"
    traj = _sample_trajectory()

    writer.write_trajectory(trial_dir, traj)
    writer.write_tool_log(trial_dir, traj)
    writer.write_task(trial_dir, {"task_id": traj.task_id, "trial_index": 0})
    writer.write_env(trial_dir, {"final": "state"})
    writer.write_metrics(trial_dir, traj)
    writer.write_grade(
        trial_dir,
        Grade(binary_pass=True, score=1.0, components=GradeComponents(state_checks=1.0)),
    )
    writer.write_logs(trial_dir, StructuredLogger("t"))

    for name in (
        "trajectory.yaml",
        "tool_log.yaml",
        "task.yaml",
        "env.yaml",
        "metrics.yaml",
        "grade.yaml",
        "logs.yaml",
    ):
        assert (trial_dir / name).exists(), f"missing {name}"


# ---------------------------------------------------------------------------
# read_recorded_tool_log — the two records-less states are not the same state
# ---------------------------------------------------------------------------


def test_a_bundle_with_no_tool_log_reads_back_as_carrying_no_record(tmp_path: Path) -> None:
    """Every bundle written before the record was persisted looks like this, forever.

    Absence is not an error and not an empty trial — it is missing evidence, and the
    caller is told so by the flag rather than having to infer it from an empty list.
    """
    FileArtifactWriter().write_trajectory(tmp_path, _sample_trajectory())

    assert read_recorded_tool_log(tmp_path) == ([], False)


def test_a_trial_that_called_no_tool_reads_back_present_and_empty(tmp_path: Path) -> None:
    """The distinction the timeline's own flag cannot make.

    ``TrialTimeline.records_present`` is ``bool(recorded_calls)``, so it reads
    ``False`` for both a bundle that recorded nothing and a trial that did nothing —
    which is why a report must carry the reader's flag and not the timeline's.
    """
    writer = FileArtifactWriter()
    trajectory = _sample_trajectory()
    assert trajectory.tool_log == []

    writer.write_tool_log(tmp_path, trajectory)

    assert read_recorded_tool_log(tmp_path) == ([], True)
    timeline = build_trial_timeline(trajectory.messages, trajectory.tool_log, None)
    assert timeline.records_present is False


def test_the_record_survives_the_round_trip_field_for_field(tmp_path: Path) -> None:
    """Including the four fields no message view could ever carry.

    ``status`` a transcript words differently on each substrate, and ``executor``,
    ``latency_seconds`` and ``sequence`` are not conversational facts at all — who
    ran a call, how long it took, and where it sits in trial-wide order across
    executors. The fixture varies all four so equality is not satisfied by a
    constant, and its multiline ``output`` is the shape YAML is most likely to
    reformat.
    """
    writer = FileArtifactWriter()
    trajectory = _sample_trajectory()
    trajectory.tool_log = [
        recorded_call("get_user", sequence=0, output="{'id': 7}", latency_seconds=0.25),
        recorded_call(
            "create_order",
            sequence=1,
            status=ToolExecutionStatus.ERROR,
            arguments={"lot": 7},
            output="Status: 409\nResponse (JSON):\n{'detail': 'conflict'}",
            latency_seconds=1.5,
        ),
        recorded_call(
            "confirm",
            sequence=2,
            executor=ToolExecutorIdentity.USER,
            output="ok",
            latency_seconds=0.75,
        ),
    ]

    writer.write_tool_log(tmp_path, trajectory)

    assert read_recorded_tool_log(tmp_path) == (trajectory.tool_log, True)


def test_an_unreadable_tool_log_names_the_file_it_could_not_read(tmp_path: Path) -> None:
    """A corrupt record is loud. Reading it as an empty one would report a trial that
    made no call, which is a claim about the trial rather than about the file."""
    (tmp_path / "tool_log.yaml").write_text("- sequence: 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"tool_log\.yaml is not a readable tool-call record"):
        read_recorded_tool_log(tmp_path)


def test_a_tool_log_that_is_not_a_list_names_what_it_parsed_as(tmp_path: Path) -> None:
    (tmp_path / "tool_log.yaml").write_text("call_id: c1\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"must be a YAML list of recorded calls.*dict"):
        read_recorded_tool_log(tmp_path)
