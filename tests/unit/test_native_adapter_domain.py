"""Adapter-level tests for the shared-domain layout.

Complements ``tests/unit/test_task_loader.py`` (which covers the pure helpers)
by exercising :class:`NativeAdapter` end-to-end on a tmp_path domain fixture:
discovery, get_task / get_task_dir, and ``_bundle_task_artifacts``.

Each test builds a tiny hermetic fixture so changes are obvious in the
fail diff.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


@pytest.fixture
def domain_fixture(tmp_path: Path) -> Path:
    """Build a minimal domain-layout fixture and return the dataset root.

    Layout::

        tmp_path/dataset/dom/
            _shared/
                domain.yaml
                mcp_server.py
                system_prompt.md
            testcases/
                case_a/
                    task.yaml
                    grading.yaml
                    initial_state.json
                case_b/
                    ...
    """
    root = tmp_path / "dataset" / "dom"

    shared = root / "_shared"
    write_yaml_file(
        shared / "domain.yaml",
        {
            "category": "tool_use",
            "tools": {
                "agent": {"mcp_server": "mcp_server.py", "enabled": ["t1"]},
                "user": {"enabled": []},
            },
            "user_simulator": {"mode": "llm", "persona": "cooperative"},
            "system_prompt": "system_prompt.md",
        },
    )
    (shared / "mcp_server.py").write_text("# stub\n")
    (shared / "system_prompt.md").write_text("Be helpful.\n")

    for case in ("case_a", "case_b"):
        case_dir = root / "testcases" / case
        case_dir.mkdir(parents=True)
        (case_dir / "initial_state.json").write_text("{}")
        write_yaml_file(
            case_dir / "task.yaml",
            {
                "task_id": f"dom_{case}",
                "name": f"dom {case}",
                "description": f"dom {case}",
                "domain": "../../_shared/domain.yaml",
                "initial_state": {"json_db": "initial_state.json"},
                "grading": "grading.yaml",
            },
        )
        write_yaml_file(
            case_dir / "grading.yaml",
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0},
                    "pass_threshold": 1.0,
                },
            },
        )
    return root


def _adapter_over(root: Path) -> NativeAdapter:
    return NativeAdapter(
        {
            "base_dir": str(root.parent),
            "tasks_glob": f"{root.name}/testcases/**/task.yaml",
        }
    )


# ---------------------------------------------------------------------------
# Discovery + get_task_dir
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discovers_both_cases(self, domain_fixture: Path) -> None:
        adapter = _adapter_over(domain_fixture)
        assert sorted(adapter.get_task_ids()) == ["dom_case_a", "dom_case_b"]

    def test_get_task_dir_returns_domain_root(self, domain_fixture: Path) -> None:
        # F1 regression. The original PR's unreachable nested ``if`` left
        # _task_roots unpopulated so this returned the case dir, not the
        # domain root.
        adapter = _adapter_over(domain_fixture)
        for case in ("dom_case_a", "dom_case_b"):
            assert adapter.get_task_dir(case) == domain_fixture

    def test_get_task_returns_merged_config(self, domain_fixture: Path) -> None:
        adapter = _adapter_over(domain_fixture)
        task = adapter.get_task("dom_case_a")
        # Domain-supplied fields surface on the merged TaskConfig.
        assert task.category == "tool_use"
        assert task.system_prompt == "_shared/system_prompt.md"
        assert task.tools.agent["mcp_server"] == "_shared/mcp_server.py"
        # Case-supplied fields are case-relative within the task_root frame.
        assert task.initial_state.json_db == "testcases/case_a/initial_state.json"
        assert task.grading == "testcases/case_a/grading.yaml"


# ---------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------


class TestBundleArtifacts:
    def test_bundle_walks_full_domain(self, domain_fixture: Path) -> None:
        # Recursive globs are the contract for the shared-domain layout —
        # ``_shared/`` siblings AND ``testcases/<case>/`` files must both end
        # up in the artifact dict so the Docker Runner reproduces the layout
        # under its temp dir.
        adapter = _adapter_over(domain_fixture)
        artifacts = adapter._bundle_task_artifacts(domain_fixture)
        keys = set(artifacts.keys())

        # _shared/ side
        assert "_shared/mcp_server.py" in keys
        assert "_shared/system_prompt.md" in keys
        assert "_shared/domain.yaml" in keys

        # Each case dir contributes its own files.
        for case in ("case_a", "case_b"):
            assert f"testcases/{case}/task.yaml" in keys
            assert f"testcases/{case}/grading.yaml" in keys
            assert f"testcases/{case}/initial_state.json" in keys

    def test_bundle_keys_are_unique_under_pattern_overlap(self, domain_fixture: Path) -> None:
        # The new bundler iterates over both ``*.yaml`` and ``**/*.yaml`` —
        # without the ``rel_path in artifacts`` guard a top-level YAML would
        # be encoded twice. Smoke-test that values are well-formed.
        adapter = _adapter_over(domain_fixture)
        artifacts = adapter._bundle_task_artifacts(domain_fixture)
        for key, val in artifacts.items():
            # Values are valid base64 of the on-disk file.
            decoded = base64.b64decode(val)
            assert decoded == (domain_fixture / key).read_bytes(), key


# ---------------------------------------------------------------------------
# Pre-loaded single-task adapter bridge (run_trial seam)
# ---------------------------------------------------------------------------


@pytest.fixture
def flat_pack_fixture(tmp_path: Path) -> Path:
    """A flat-layout pack (no MCP server) whose task dir equals the task.yaml
    parent, so ``to_task_description`` resolves its assets without spawning a
    tool-schema subprocess. Returns the base dir the adapter globs from."""
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("Be helpful.\n")
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
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
                "pass_threshold": 1.0,
            },
        },
    )
    return tmp_path


class TestPreloadedTaskBridge:
    def test_preloaded_adapter_resolves_assets_identically(self, flat_pack_fixture: Path) -> None:
        # A pre-loaded, source-dir-stamped TaskConfig registered on a fresh
        # adapter must resolve to the SAME TaskDescription + GradingConfig as a
        # normally-discovered adapter over the same pack — proving the bridge
        # reuses asset resolution rather than reimplementing it. ``generated_at``
        # is a per-call timestamp, so it is excluded from the comparison.
        discovered = NativeAdapter(
            {"base_dir": str(flat_pack_fixture), "tasks_glob": "tasks/**/task.yaml"}
        )
        task = discovered.get_task("flat")
        expected_td = discovered.to_task_description("flat")
        expected_grading = discovered.get_grading_config("flat")

        assert task.source_dir == flat_pack_fixture / "tasks" / "flat"

        preloaded = NativeAdapter({"tasks_glob": "unused/**/task.yaml"})
        preloaded.register_preloaded_task(task, task.source_dir)

        assert preloaded.get_task("flat") is task
        assert preloaded.get_task_dir("flat") == task.source_dir

        actual_td = preloaded.to_task_description("flat")
        assert actual_td.model_dump(exclude={"generated_at"}) == expected_td.model_dump(
            exclude={"generated_at"}
        )
        assert preloaded.get_grading_config("flat") == expected_grading


# ---------------------------------------------------------------------------
# Flat-layout regression — domain support must not break the existing path.
# ---------------------------------------------------------------------------


def test_flat_layout_get_task_dir_is_task_parent(tmp_path: Path) -> None:
    """A flat-layout task without ``domain:`` keeps the legacy contract:
    ``get_task_dir`` returns ``task_path.parent``."""
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("hi\n")
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "user_simulator": {"mode": "llm", "persona": "cooperative"},
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
                "pass_threshold": 1.0,
            },
        },
    )

    adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
    assert adapter.get_task_dir("flat") == task_dir
    task = adapter.get_task("flat")
    # Flat layout: paths stay verbatim from task.yaml.
    assert task.system_prompt == "system_prompt.md"
    assert task.grading == "grading.yaml"
