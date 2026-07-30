"""``NativeAdapter.to_task_description`` delivers ``checks.py`` to the runner
and propagates ``grading.custom_checks`` onto the wire.

A pack whose ``grading.yaml`` sets ``custom_checks.enabled: true`` requires two
things at trial-launch time: (1) ``checks.py`` and every ``relative_imports``
target must land in ``TaskDescription.tool_artifacts`` so the runner can
extract them into the trial's ``artifacts_dir``, and (2) the ``custom_checks``
config block must ride the wire ``GradingConfig.custom_checks`` so
``RunnerService._grade_custom_checks`` reads a real config rather than
short-circuiting. Both must hold whether the pack ships an MCP server or not.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


_CHECKS_PY = """from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, check, init,
)


@init(interface_version="1.0")
def setup(ctx: CheckContext) -> None:
    pass


@check(name="always_ok")
def always_ok(ctx: CheckContext) -> CheckPassed:
    return CheckPassed(message="ok")
"""


_HELPERS_PY = "SHARED_MARKER = 'from-helpers'\n"


def _mcp_less_pack(
    tmp_path: Path,
    *,
    custom_checks: dict | None,
    include_helpers_import: bool = False,
) -> NativeAdapter:
    """Build an MCP-less pack — no ``mcp_server`` key, no enabled agent tools.

    When *custom_checks* is None the pack has no ``custom_checks:`` block; the
    adapter must ship an empty ``tool_artifacts`` (regression lock on the
    no-op path).
    """
    task_dir = tmp_path / "tasks" / "cc_pack"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    (task_dir / "checks.py").write_text(_CHECKS_PY)
    if include_helpers_import:
        helpers_dir = task_dir / "helpers"
        helpers_dir.mkdir()
        (helpers_dir / "shared.py").write_text(_HELPERS_PY)

    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "cc_pack",
            "name": "cc_pack",
            "category": "tool_use",
            "description": "custom checks pack",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    grading: dict = {
        "combine": {
            "method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
        },
    }
    if custom_checks is not None:
        grading["custom_checks"] = custom_checks
    write_yaml_file(task_dir / "grading.yaml", grading)
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


def _mcp_pack(tmp_path: Path, *, custom_checks: dict) -> NativeAdapter:
    """Build an MCP pack — ``mcp_server.py`` present, ``fixtures/tools.json``
    pre-baked so ``to_task_description`` avoids the live MCP subprocess.
    """
    task_dir = tmp_path / "tasks" / "cc_mcp_pack"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    (task_dir / "mcp_server.py").write_text("# stub\n")
    (task_dir / "checks.py").write_text(_CHECKS_PY)
    (task_dir / "fixtures").mkdir()
    (task_dir / "fixtures" / "tools.json").write_text(json.dumps([]))

    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "cc_mcp_pack",
            "name": "cc_mcp_pack",
            "category": "tool_use",
            "description": "custom checks + MCP pack",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {
                "agent": {"mcp_server": "mcp_server.py", "enabled": []},
                "user": {"enabled": []},
            },
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"custom_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "custom_checks": custom_checks,
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


class TestMcpLessBundleAndPropagate:
    def test_enabled_bundles_checks_and_propagates_config(self, tmp_path: Path) -> None:
        cc_config = {
            "enabled": True,
            "file": "checks.py",
            "interface_version": "1.0",
            "timeout_seconds": 10,
            "weight": 1.0,
            "fail_on_error": True,
            "relative_imports": ["helpers"],
        }
        adapter = _mcp_less_pack(tmp_path, custom_checks=cc_config, include_helpers_import=True)
        td = adapter.to_task_description("cc_pack")

        # (1) checks.py + relative_imports target ship as tool_artifacts.
        assert "checks.py" in td.tool_artifacts
        assert "helpers/shared.py" in td.tool_artifacts
        decoded = base64.b64decode(td.tool_artifacts["checks.py"]).decode()
        assert "@check" in decoded
        assert "always_ok" in decoded

        # (2) The custom_checks config rides the wire GradingConfig so the
        # runner reads a real dict rather than short-circuiting on None.
        assert td.grading.custom_checks == cc_config

    def test_disabled_ships_no_artifacts_and_no_config(self, tmp_path: Path) -> None:
        # Regression lock: the empty-tool_artifacts baseline for MCP-less
        # packs must survive the Stage 3 un-gate. Only ``custom_checks.enabled``
        # should flip bundling on.
        adapter = _mcp_less_pack(tmp_path, custom_checks=None)
        td = adapter.to_task_description("cc_pack")

        assert td.tool_artifacts == {}
        assert td.grading.custom_checks is None

    def test_enabled_false_ships_no_artifacts(self, tmp_path: Path) -> None:
        # A ``custom_checks:`` block with ``enabled: false`` is a bystander —
        # the pack does not need ``checks.py`` shipped, and the wire config
        # still carries the dict so downstream consumers see the author's
        # explicit opt-out (rather than an ambiguous ``None``).
        cc_config = {"enabled": False, "file": "checks.py"}
        adapter = _mcp_less_pack(tmp_path, custom_checks=cc_config)
        td = adapter.to_task_description("cc_pack")

        assert td.tool_artifacts == {}
        assert td.grading.custom_checks == cc_config


class TestMcpPackNoRegression:
    def test_mcp_pack_still_bundles_and_propagates(self, tmp_path: Path) -> None:
        cc_config = {
            "enabled": True,
            "file": "checks.py",
            "interface_version": "1.0",
            "relative_imports": [],
        }
        adapter = _mcp_pack(tmp_path, custom_checks=cc_config)
        td = adapter.to_task_description("cc_mcp_pack")

        # MCP tasks bundle the whole task dir already; adding custom_checks
        # must not disturb that — checks.py and mcp_server.py both ride.
        assert "checks.py" in td.tool_artifacts
        assert "mcp_server.py" in td.tool_artifacts

        # Config propagates on the MCP path too.
        assert td.grading.custom_checks == cc_config
