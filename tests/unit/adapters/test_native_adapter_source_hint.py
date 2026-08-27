"""``NativeAdapter._actor_tool_schemas`` refuses a source-less non-builtin tool.

Locks the emit-time raise: when the block declares no ``mcp_server`` and the
enabled name is not a builtin (and not ``search_kb``), NativeAdapter raises
:class:`NativeAdapterMisconfigurationError` before appending the source-less
schema. The message carries the tool name, pack root, ``harness_adapter``
config key, and the enumerated non-native adapter list. When the pack sits
alongside the generic filesystem signature ``_domain/tools/**`` (walk ≤3
levels), the message also adds a ``detected shape:`` clause naming the matched
relative path — a filesystem pattern, never a hardcoded pack name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import (
    NativeAdapter,
    NativeAdapterMisconfigurationError,
)

pytestmark = pytest.mark.unit


def _write_task_yaml(
    task_dir: Path,
    *,
    agent_tools: dict,
) -> None:
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "src_less_pack",
            "name": "src_less_pack",
            "category": "tool_use",
            "description": "source-less pack",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": agent_tools, "user": {"enabled": []}},
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
                "weights": {"state_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )


def _adapter(tmp_path: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "**/task.yaml"})


class TestNativeAdapterSourceHint:
    def test_source_less_non_builtin_raises_with_actionable_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``_domain/`` sibling: raise fires without a ``detected shape:`` clause."""
        task_dir = tmp_path / "tasks" / "src_less_pack"
        task_dir.mkdir(parents=True)
        _write_task_yaml(task_dir, agent_tools={"enabled": ["frozen_domain_tool_x"]})
        # Pin the enumeration so the assertion doesn't drift with the local
        # entry-point set on any CI machine.
        monkeypatch.setattr(
            "tolokaforge.adapters.available_adapters",
            lambda: ["native", "fake_plugin_a", "fake_plugin_b"],
        )

        with pytest.raises(NativeAdapterMisconfigurationError) as exc_info:
            _adapter(tmp_path).to_task_description("src_less_pack")

        message = str(exc_info.value)
        assert "frozen_domain_tool_x" in message
        assert str(task_dir) in message
        assert "evaluation.harness_adapter.type" in message
        for adapter in ("fake_plugin_a", "fake_plugin_b"):
            assert adapter in message
        # No signature match — the generic hint stands alone.
        assert "detected shape:" not in message

    def test_detected_shape_clause_names_domain_tools_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_domain/tools/<name>`` sibling: raise adds a ``detected shape:`` clause."""
        pack_root = tmp_path / "pack_root"
        task_dir = pack_root / "tasks" / "src_less_pack"
        task_dir.mkdir(parents=True)
        (pack_root / "_domain" / "tools" / "mcp_tools_library").mkdir(parents=True)
        _write_task_yaml(task_dir, agent_tools={"enabled": ["frozen_domain_tool_x"]})
        monkeypatch.setattr(
            "tolokaforge.adapters.available_adapters",
            lambda: ["native", "fake_plugin_a"],
        )

        with pytest.raises(NativeAdapterMisconfigurationError) as exc_info:
            _adapter(tmp_path).to_task_description("src_less_pack")

        message = str(exc_info.value)
        assert "detected shape: _domain/tools/mcp_tools_library" in message

    def test_builtin_tool_never_raises_emit_time_hint(self, tmp_path: Path) -> None:
        """A source-less builtin (``bash``) stays on the happy path."""
        task_dir = tmp_path / "tasks" / "src_less_pack"
        task_dir.mkdir(parents=True)
        _write_task_yaml(task_dir, agent_tools={"enabled": ["bash"]})

        td = _adapter(tmp_path).to_task_description("src_less_pack")

        names = {t.name for t in td.agent_tools}
        assert "bash" in names

    def test_mcp_server_declared_never_raises_emit_time_hint(self, tmp_path: Path) -> None:
        """Declaring ``mcp_server`` clears the check — source metadata is emitted."""
        task_dir = tmp_path / "tasks" / "src_less_pack"
        task_dir.mkdir(parents=True)
        _write_task_yaml(
            task_dir,
            agent_tools={
                "mcp_server": "mcp_server.py",
                "enabled": ["frozen_domain_tool_x"],
            },
        )
        (task_dir / "mcp_server.py").write_text("# stub\n")
        # Pre-baked fixture bypasses the live MCP subprocess handshake that
        # ``resolve_tool_schemas`` would otherwise attempt against the stub.
        (task_dir / "fixtures").mkdir()
        (task_dir / "fixtures" / "tools.json").write_text(json.dumps([]))

        td = _adapter(tmp_path).to_task_description("src_less_pack")

        names = {t.name for t in td.agent_tools}
        assert "frozen_domain_tool_x" in names
