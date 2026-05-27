"""``tolokaforge.core.output.artifacts`` unit tests.

Covers:

* :func:`model_id_slug` — deterministic, filesystem-safe slug over every
  real-world model name registered in
  [`model_presets.yaml`](../../tolokaforge/core/data/model_presets.yaml).
* :class:`FileArtifactWriter.write_tools_schemas` — per-trial YAML
  artifact at ``trial_dir/tools_schemas.yaml``. Latest write wins
  (the trial dir is recreated fresh by the orchestrator).
* Protocol conformance — :class:`FileArtifactWriter` satisfies the
  :class:`TrialArtifactWriter` structural type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    TrialArtifactWriter,
    model_id_slug,
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
        "task.yaml",
        "env.yaml",
        "metrics.yaml",
        "grade.yaml",
        "logs.yaml",
    ):
        assert (trial_dir / name).exists(), f"missing {name}"
