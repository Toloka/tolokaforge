"""The shipped ``examples/native/coding_harness/`` pack is a whole document
of its own kind: a native-adapter run config declaring
``models.agent.coding_harness`` end-to-end, proving the "any adapter" claim of the
coding-harness lift with something executable.

The run config surviving :class:`RunConfig` validation is the first contract
this suite locks. The second is the four-key harness metadata handshake the
conductor branches on — a snapshot of the wire dict a run of the pack under
``claude-code`` produces, so a byte-level drift in the mixin's command
assembly or the adapter's metadata dict shape shows up here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import RunConfig

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "coding_harness"
_RUN_CONFIG_PATH = _PACK_ROOT / "run_harness.yaml"


def test_run_harness_yaml_loads_as_run_config() -> None:
    """The pack's exemplar run config validates cleanly."""
    payload = yaml.safe_load(_RUN_CONFIG_PATH.read_text())
    cfg = RunConfig(**payload)
    agent = cfg.models.get("agent")
    assert agent is not None
    assert agent.coding_harness == "claude-code"
    assert cfg.evaluation.harness_adapter is not None
    assert cfg.evaluation.harness_adapter.type == "native"
    assert cfg.orchestrator.strict_task_load is True


def test_native_adapter_builds_harness_task_description() -> None:
    """The native adapter loads the pack task cleanly under harness mode."""
    adapter = NativeAdapter(
        {
            "tasks_glob": "task.yaml",
            "task_packs": [str(_PACK_ROOT)],
            "agent_harness": "claude-code",
            "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
        }
    )
    assert adapter.get_task_ids() == ["fix_factorial"]
    td = adapter.to_task_description("fix_factorial")
    # Single bash tool via docker_compose_exec — the shape the runner-side
    # DockerComposeExecToolWrapper factory dispatches on.
    assert len(td.agent_tools) == 1
    assert td.agent_tools[0].name == "bash"
    assert td.agent_tools[0].source is not None
    assert td.agent_tools[0].source.invocation_style == "docker_compose_exec"
    # test_execution grading — the runner reads the reward from
    # /logs/verifier/reward.txt inside the trial container.
    assert td.grading.grading_method == "test_execution"


def test_harness_metadata_snapshot(canon_snapshot) -> None:
    """Snapshot the four-key harness metadata handshake.

    The dict crosses the wire on ``TaskDescription.metadata`` and the
    conductor's ``_run_agent_loop`` branches on ``agent_harness_command``;
    drift in any of the four keys is a wire break. Version-bump coordination
    is handled by the same commit that bumps the harness registry — the
    ``harness_registry_replay`` metric moves in lockstep with this snapshot.
    """
    adapter = NativeAdapter(
        {
            "tasks_glob": "task.yaml",
            "task_packs": [str(_PACK_ROOT)],
            "agent_harness": "claude-code",
            "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
        }
    )
    td = adapter.to_task_description("fix_factorial")
    metadata = {
        key: td.metadata[key]
        for key in (
            "agent_harness",
            "agent_harness_version",
            "agent_harness_model",
            "agent_harness_command",
        )
    }
    snap = canon_snapshot("native_coding_harness_fix_factorial")
    snap.assert_match(metadata, "harness_metadata.json")
