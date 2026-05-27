"""Canonical directory-layout snapshot for per-trial outputs.

Locks the on-disk layout the orchestrator emits so accidental drift
(renamed file, missing artifact, reintroduced legacy sidecar) fails CI
loudly.

Instead of driving a real Docker trial (needs running services + task
fixtures), this test exercises the exact writer contract the
orchestrator uses: one :class:`FileArtifactWriter` shared across trials,
one ``trials/<task>/<n>/`` directory per trial with eight YAML files
each. ``tools_schemas.yaml`` and ``prompts.yaml`` live **inside** the
trial bundle alongside ``trajectory.yaml`` — every trial is
self-contained.

Scenario
--------
Four trials, two tasks × two agent models::

    (task_A, model1) → trials 0 + 1
    (task_B, model1) → trial  0
    (task_A, model2) → trial  2

Expected layout::

    results/
      trials/
        task_A/
          0/{env,grade,logs,metrics,prompts,task,tools_schemas,trajectory}.yaml
          1/...
          2/...
        task_B/
          0/...

Spot-checks (content — not full snapshot; that lives in
:mod:`tests.canonical.test_trajectory_reasoning_snapshot`):

* ``task.yaml.model_config.agent.resolved.effective_preset`` = preset name.
* ``task.yaml.model_config.agent.resolved.cache_policy`` = registered name.
* ``prompts.yaml`` carries the agent + user-simulator system prompts.
* ``trajectory.yaml`` does NOT carry the prompts (moved to ``prompts.yaml``).
* ``tools_schemas.yaml`` equals ``capabilities.schema_sanitizer.sanitize(tools)``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    ModelConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import _build_resolved_block
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _raw_tools() -> list[dict]:
    """Tool schemas with ``Dict[str, …]`` + Decimal shapes — targets that
    the ``strict`` sanitizer actively rewrites (P1 / P2). Guarantees that
    the sidecar file differs between a ``strict`` and ``passthrough``
    preset — a content-shape assertion can catch a regression where we
    accidentally write raw tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "place_order",
                "description": "Place an order.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "total": {
                            "anyOf": [
                                {"type": "number"},
                                {
                                    "type": "string",
                                    "pattern": "^(?!^[-+.]*$)(?:\\d+)$",
                                },
                            ],
                            "description": "Grand total (Decimal).",
                        },
                        "lines": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "sku": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                },
                                "required": ["sku", "quantity"],
                            },
                            "description": "Map of sku → OrderLine.",
                        },
                    },
                    "required": ["total", "lines"],
                },
            },
        }
    ]


def _trajectory(task_id: str, trial_index: int) -> Trajectory:
    """Build a minimal Trajectory for the layout snapshot.

    System prompts are no longer carried on Trajectory — they're written
    separately by :meth:`FileArtifactWriter.write_prompts`. This helper
    therefore takes only the identifiers; the per-trial prompts are
    handed to ``write_prompts`` directly inside :func:`_drive_trial`.
    """
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="Hello", ts=ts),
            Message(role=MessageRole.ASSISTANT, content="Done.", ts=ts),
        ],
    )


def _task_snapshot(
    task_id: str,
    trial_index: int,
    agent_config: ModelConfig,
    user_config: ModelConfig | None,
) -> dict:
    """Mirror the orchestrator's ``task.yaml`` payload shape."""
    agent_dict = agent_config.model_dump(mode="json")
    agent_dict["resolved"] = _build_resolved_block(agent_config)

    user_dict: dict | None = None
    if user_config is not None:
        user_dict = user_config.model_dump(mode="json")
        user_dict["resolved"] = _build_resolved_block(user_config)

    return {
        "task_id": task_id,
        "trial_index": trial_index,
        "category": "canonical",
        "description": f"Trial {trial_index} of {task_id}",
        "grading_config": {},
        "tools": {"agent": {}, "user": {}},
        "policies": {},
        "model_config": {"agent": agent_dict, "user": user_dict},
    }


_AGENT_PROMPT = "Agent sys prompt."
_USER_SIM_PROMPT = "You are a user interacting with an agent."


def _drive_trial(
    writer: FileArtifactWriter,
    results_root: Path,
    task_id: str,
    trial_index: int,
    agent_config: ModelConfig,
    user_config: ModelConfig | None,
) -> None:
    """One orchestrator-style trial write: eight YAML files inside the
    trial bundle (``tools_schemas.yaml`` and ``prompts.yaml`` included)."""
    capabilities = build_capabilities(
        agent_config.name, agent_config.provider, overrides=agent_config.capabilities
    )
    trial_dir = results_root / "trials" / task_id / str(trial_index)
    sim_prompt = _USER_SIM_PROMPT if user_config else None

    sanitized = capabilities.schema_sanitizer.sanitize(_raw_tools())
    writer.write_tools_schemas(trial_dir, sanitized)
    writer.write_prompts(trial_dir, agent_prompt=_AGENT_PROMPT, user_prompt=sim_prompt)
    writer.write_trajectory(trial_dir, _trajectory(task_id, trial_index))
    writer.write_task(trial_dir, _task_snapshot(task_id, trial_index, agent_config, user_config))
    writer.write_env(trial_dir, {"final_state": {"trial": trial_index}})
    writer.write_metrics(trial_dir, _trajectory(task_id, trial_index))
    writer.write_grade(trial_dir, Grade(binary_pass=True, score=1.0, components=GradeComponents()))
    writer.write_logs(trial_dir, StructuredLogger(f"{task_id}-{trial_index}"))


# ---------------------------------------------------------------------------
# The canonical directory-layout test
# ---------------------------------------------------------------------------


_AGENT1 = ModelConfig(provider="openai", name="gpt-5.5")
_AGENT2 = ModelConfig(provider="anthropic", name="claude-opus-4.7")
_USER_SIM = ModelConfig(provider="openai", name="user-sim-mock")


def _relative_file_tree(root: Path) -> list[str]:
    """Return sorted list of files (relative POSIX paths) under *root*."""
    return sorted(str(p.relative_to(root).as_posix()) for p in root.rglob("*") if p.is_file())


def test_output_directory_layout_snapshot(tmp_path: Path) -> None:
    """Four trials, 2 × 2 (task, model) matrix → exact expected layout."""
    writer = FileArtifactWriter()

    # task_A × model1 × two trials
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)
    _drive_trial(writer, tmp_path, "task_A", 1, _AGENT1, _USER_SIM)
    # task_B × model1 × one trial
    _drive_trial(writer, tmp_path, "task_B", 0, _AGENT1, _USER_SIM)
    # task_A × model2 × one trial
    _drive_trial(writer, tmp_path, "task_A", 2, _AGENT2, _USER_SIM)

    trial_yamls = [
        "env.yaml",
        "grade.yaml",
        "logs.yaml",
        "metrics.yaml",
        "prompts.yaml",
        "task.yaml",
        "tools_schemas.yaml",
        "trajectory.yaml",
    ]
    expected = sorted(
        [f"trials/task_A/0/{n}" for n in trial_yamls]
        + [f"trials/task_A/1/{n}" for n in trial_yamls]
        + [f"trials/task_A/2/{n}" for n in trial_yamls]
        + [f"trials/task_B/0/{n}" for n in trial_yamls]
    )

    actual = _relative_file_tree(tmp_path)
    assert actual == expected
    # Legacy results-root sidecar trees must NOT appear.
    assert not (tmp_path / "tools_schemas").exists()
    assert not (tmp_path / "prompts").exists()


# ---------------------------------------------------------------------------
# Content spot-checks (per plan § 7j)
# ---------------------------------------------------------------------------


def test_task_yaml_carries_resolved_preset(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT2, _USER_SIM)

    task_yaml = tmp_path / "trials" / "task_A" / "0" / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text())
    resolved = data["model_config"]["agent"]["resolved"]

    assert resolved["effective_preset"] == "anthropic_claude_4_7"
    assert resolved["cache_policy"] == "anthropic_ephemeral"
    assert resolved["schema_sanitizer"] == "passthrough"
    assert resolved["reasoning_codec"] == "anthropic"


def test_task_yaml_carries_resolved_preset_for_user_simulator(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)

    task_yaml = tmp_path / "trials" / "task_A" / "0" / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text())
    user_resolved = data["model_config"]["user"]["resolved"]

    # ``_USER_SIM`` = openai + "user-sim-mock" — falls through to default.
    assert user_resolved["effective_preset"] == "default"
    assert user_resolved["cache_policy"] == "none"


def test_prompts_yaml_carries_both_system_prompts(tmp_path: Path) -> None:
    """``prompts.yaml`` is now the home of both the agent system prompt
    and the user simulator's system prompt — moved out of
    ``trajectory.yaml`` to keep the message trace lean."""
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)

    prompts_path = tmp_path / "trials" / "task_A" / "0" / "prompts.yaml"
    data = yaml.safe_load(prompts_path.read_text())

    assert data == {
        "system_prompt": _AGENT_PROMPT,
        "user_system_prompt": _USER_SIM_PROMPT,
    }


def test_trajectory_yaml_does_not_carry_prompts(tmp_path: Path) -> None:
    """``trajectory.yaml`` must not carry ``system_prompt`` or
    ``user_system_prompt`` — those moved to ``prompts.yaml``. The
    trajectory keeps only the message trace + status + metrics."""
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)

    traj_yaml = tmp_path / "trials" / "task_A" / "0" / "trajectory.yaml"
    data = yaml.safe_load(traj_yaml.read_text())

    assert (
        "system_prompt" not in data
    ), "trajectory.yaml must not carry the agent system prompt — moved to prompts.yaml"
    assert (
        "user_system_prompt" not in data
    ), "trajectory.yaml must not carry the user simulator prompt — moved to prompts.yaml"
    # ``simulator_schema_version`` stays — it's metadata about the
    # message-trace shape, not a prompt itself.
    assert data["simulator_schema_version"] == 1


def test_tools_schemas_yaml_matches_sanitizer_output(tmp_path: Path) -> None:
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)

    schema_path = tmp_path / "trials" / "task_A" / "0" / "tools_schemas.yaml"
    on_disk = yaml.safe_load(schema_path.read_text())

    capabilities = build_capabilities(_AGENT1.name, _AGENT1.provider)
    # The sanitizer's output is what the canonical file must equal — no
    # raw Pydantic schemas should leak onto disk.
    expected = capabilities.schema_sanitizer.sanitize(_raw_tools())
    assert on_disk == expected

    # GPT-5 preset is ``strict`` — Decimal ``anyOf`` pattern is stripped.
    dumped = schema_path.read_text()
    assert "pattern" not in dumped
    assert "(?!" not in dumped  # RE2-hostile lookaround eliminated.


def test_tools_schemas_yaml_per_trial_no_dedup(tmp_path: Path) -> None:
    """Every trial dir gets its own ``tools_schemas.yaml`` — no
    cross-trial sharing or filename dedup."""
    writer = FileArtifactWriter()
    _drive_trial(writer, tmp_path, "task_A", 0, _AGENT1, _USER_SIM)
    _drive_trial(writer, tmp_path, "task_A", 1, _AGENT1, _USER_SIM)
    _drive_trial(writer, tmp_path, "task_A", 2, _AGENT1, _USER_SIM)

    for trial_idx in (0, 1, 2):
        path = tmp_path / "trials" / "task_A" / str(trial_idx) / "tools_schemas.yaml"
        assert path.exists()
        # All three carry the same payload (same task, same model) but as
        # independent files — no symlinks, no dedup.
        assert not path.is_symlink()
