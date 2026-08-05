"""Unit tests for tolokaforge.adapters._task_loader.

Covers the shared-domain merge that lets ``<dom>/testcases/<case>/task.yaml``
inherit fields from ``<dom>/_shared/domain.yaml``. Each test builds a tiny
hermetic fixture under ``tmp_path`` so the layout choices are explicit and
not coupled to any in-tree task pack.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.adapters._task_loader import (
    GradingSource,
    GradingSourceKind,
    _detect_task_root,
    _load_domain_dict,
    _rewrite_task_paths,
    deep_merge,
    grading_source_under_adapter,
    load_task_yaml,
)
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _make_domain_layout(root: Path, *, case: str = "case_a") -> Path:
    """Build a minimal valid domain-layout fixture under *root* and return the
    case task.yaml path."""
    shared = root / "demo" / "_shared"
    case_dir = root / "demo" / "testcases" / case

    _write_yaml(
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

    (case_dir / "initial_state.json").parent.mkdir(parents=True, exist_ok=True)
    (case_dir / "initial_state.json").write_text("{}")
    _write_yaml(
        case_dir / "task.yaml",
        {
            "task_id": f"demo_{case}",
            "name": f"demo {case}",
            "description": f"demo case {case}",
            "domain": "../../_shared/domain.yaml",
            "initial_state": {"json_db": "initial_state.json"},
            "grading": "grading.yaml",
        },
    )
    _write_yaml(
        case_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            },
        },
    )
    return case_dir / "task.yaml"


def _make_flat_layout(root: Path) -> Path:
    """Build a minimal flat-layout task and return its task.yaml path."""
    task_dir = root / "flat_task"
    (task_dir / "system_prompt.md").parent.mkdir(parents=True, exist_ok=True)
    (task_dir / "system_prompt.md").write_text("hello\n")
    (task_dir / "initial_state.json").write_text("{}")
    _write_yaml(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat task",
            "category": "tool_use",
            "description": "flat task",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "user_simulator": {"mode": "llm", "persona": "cooperative"},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    _write_yaml(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            },
        },
    )
    return task_dir / "task.yaml"


# ---------------------------------------------------------------------------
# _detect_task_root
# ---------------------------------------------------------------------------


class TestDetectTaskRoot:
    def test_domain_layout_returns_domain_root(self, tmp_path: Path) -> None:
        # Regression for the original PR #81 bug: an unreachable nested ``if``
        # left _task_roots empty so domain-layout tasks silently degraded to
        # the flat layout. _detect_task_root must return <dom>, not the case
        # dir, so downstream consumers (NativeAdapter._bundle_task_artifacts)
        # bundle the _shared/ siblings.
        task_path = tmp_path / "my_dom" / "testcases" / "case_a" / "task.yaml"
        assert _detect_task_root(task_path) == tmp_path / "my_dom"

    def test_flat_layout_returns_parent(self, tmp_path: Path) -> None:
        task_path = tmp_path / "my_task" / "task.yaml"
        assert _detect_task_root(task_path) == tmp_path / "my_task"

    def test_intermediate_dir_named_testcases_does_not_trigger(self, tmp_path: Path) -> None:
        # Only <dom>/testcases/<case>/task.yaml triggers the domain heuristic.
        # A task whose grandparent is *not* literally "testcases" stays flat.
        task_path = tmp_path / "tasks" / "my_dom" / "task.yaml"
        assert _detect_task_root(task_path) == tmp_path / "tasks" / "my_dom"


# ---------------------------------------------------------------------------
# _load_domain_dict + deep_merge
# ---------------------------------------------------------------------------


class TestLoadDomainDict:
    def test_no_domain_ref_returns_empty(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        task_data = {"task_id": "x", "category": "tool_use"}
        assert _load_domain_dict(task_path, task_data, tmp_path) == {}

    def test_loads_domain_yaml_contents(self, tmp_path: Path) -> None:
        domain_path = tmp_path / "dom" / "_shared" / "domain.yaml"
        _write_yaml(
            domain_path,
            {
                "category": "domain_default",
                "tools": {"agent": {"enabled": ["t_domain"]}},
            },
        )
        task_path = tmp_path / "dom" / "testcases" / "c1" / "task.yaml"
        task_path.parent.mkdir(parents=True)
        loaded = _load_domain_dict(
            task_path,
            {"domain": "../../_shared/domain.yaml"},
            tmp_path / "dom",
        )
        assert loaded == {
            "category": "domain_default",
            "tools": {"agent": {"enabled": ["t_domain"]}},
        }

    def test_missing_domain_file_raises(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        with pytest.raises(RuntimeError, match="Domain file referenced"):
            _load_domain_dict(task_path, {"domain": "missing.yaml"}, tmp_path)

    def test_non_mapping_domain_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a\n- list\n")
        task_path = tmp_path / "task.yaml"
        with pytest.raises(RuntimeError, match="not a YAML mapping"):
            _load_domain_dict(task_path, {"domain": "bad.yaml"}, tmp_path)

    def test_domain_paths_rewritten_to_task_root(self, tmp_path: Path) -> None:
        # Domain-side ``mcp_server: mcp_server.py`` lives at <dom>/_shared/.
        # After the loader rewrites paths into the task_root frame, the
        # value must resolve to ``_shared/mcp_server.py`` from that frame.
        domain_dir = tmp_path / "dom" / "_shared"
        domain_dir.mkdir(parents=True)
        (domain_dir / "mcp_server.py").write_text("# stub\n")
        _write_yaml(
            domain_dir / "domain.yaml",
            {"tools": {"agent": {"mcp_server": "mcp_server.py"}}},
        )
        case_dir = tmp_path / "dom" / "testcases" / "c1"
        case_dir.mkdir(parents=True)
        loaded = _load_domain_dict(
            case_dir / "task.yaml",
            {"task_id": "c1", "domain": "../../_shared/domain.yaml"},
            tmp_path / "dom",
        )
        assert loaded["tools"]["agent"]["mcp_server"] == "_shared/mcp_server.py"
        assert (tmp_path / "dom" / loaded["tools"]["agent"]["mcp_server"]).exists()


class TestDeepMerge:
    """Task-side merge combines domain + task via ``deep_merge`` — task
    wins on conflict, nested dicts recurse."""

    def test_task_wins_on_scalar_conflict(self) -> None:
        domain = {"category": "domain_default"}
        task = {"category": "case_override"}
        assert deep_merge(domain, task) == {"category": "case_override"}

    def test_nested_dict_deep_merges_with_task_wins(self) -> None:
        domain = {"tools": {"agent": {"enabled": ["t_domain"]}}}
        task = {"tools": {"agent": {"enabled": ["t_case"]}}}
        assert deep_merge(domain, task) == {"tools": {"agent": {"enabled": ["t_case"]}}}


# ---------------------------------------------------------------------------
# _rewrite_task_paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_path",
    [
        ["grading"],
        ["system_prompt"],
        ["tools", "agent", "mcp_server"],
        ["tools", "user", "mcp_server"],
        ["initial_state", "json_db"],
        ["initial_state", "system_prompt"],
    ],
)
def test_rewrite_string_path_fields_covers_all(tmp_path: Path, field_path: list[str]) -> None:
    """Every relative-path string field on TaskConfig / InitialStateConfig is
    rewritten when the task root moves from one dir to another. Failure here
    means a new path field landed without an entry in
    ``_PATH_FIELD_REWRITERS`` — add it there."""
    domain_dir = tmp_path / "_shared"
    case_dir = tmp_path / "testcases" / "c1"
    domain_dir.mkdir(parents=True)
    case_dir.mkdir(parents=True)

    # Build a nested dict with a single relative path leaf.
    data: dict = {}
    node = data
    for key in field_path[:-1]:
        node[key] = {}
        node = node[key]
    node[field_path[-1]] = "thing.txt"

    # File lives next to the domain dict so the rewrite resolves.
    (domain_dir / "thing.txt").write_text("x")

    _rewrite_task_paths(data, domain_dir, case_dir)

    # Walk the same path and confirm the leaf is now case-relative.
    node2: object = data
    for key in field_path:
        assert isinstance(node2, dict)
        node2 = node2[key]
    assert node2 == "../../_shared/thing.txt"


def test_rewrite_filesystem_copy_walks_list_of_dicts(tmp_path: Path) -> None:
    """``initial_state.filesystem.copy[].from`` is a list of dicts where only
    the ``from`` key is a path. Regression for F3 — the original PR's
    rewriter missed this field entirely."""
    domain_dir = tmp_path / "_shared"
    case_dir = tmp_path / "testcases" / "c1"
    domain_dir.mkdir(parents=True)
    case_dir.mkdir(parents=True)
    (domain_dir / "seed.txt").write_text("hi")

    data = {
        "initial_state": {
            "filesystem": {
                "copy": [
                    {"from": "seed.txt", "to": "/agent/seed.txt"},
                    {"from": "seed.txt", "to": "/agent/seed2.txt"},
                ]
            }
        }
    }

    _rewrite_task_paths(data, domain_dir, case_dir)

    copies = data["initial_state"]["filesystem"]["copy"]
    assert copies[0]["from"] == "../../_shared/seed.txt"
    assert copies[0]["to"] == "/agent/seed.txt"  # ``to`` left untouched
    assert copies[1]["from"] == "../../_shared/seed.txt"


def test_rewrite_inline_json_db_dict_is_left_alone(tmp_path: Path) -> None:
    """When ``initial_state.json_db`` is an inline dict literal it has no
    path to resolve. Rewriting it would corrupt the value."""
    data = {"initial_state": {"json_db": {"orders": []}}}
    _rewrite_task_paths(data, tmp_path / "a", tmp_path / "b")
    assert data["initial_state"]["json_db"] == {"orders": []}


# ---------------------------------------------------------------------------
# load_task_yaml — end-to-end
# ---------------------------------------------------------------------------


class TestLoadTaskYaml:
    def test_domain_layout_end_to_end(self, tmp_path: Path) -> None:
        task_path = _make_domain_layout(tmp_path, case="case_a")
        task, task_dir = load_task_yaml(task_path)

        # Domain-merged values are visible on the validated TaskConfig.
        assert task.task_id == "demo_case_a"
        assert task.category == "tool_use"

        # Effective task dir is the domain root, not the case dir.
        assert task_dir == tmp_path / "demo"

        # Every relative-path field on the returned TaskConfig resolves
        # cleanly from task_dir — this is the contract test_load_task_yaml
        # callers (NativeAdapter, validate CLI) rely on.
        assert task.tools.agent["mcp_server"] == "_shared/mcp_server.py"
        assert (task_dir / task.tools.agent["mcp_server"]).exists()
        assert task.system_prompt == "_shared/system_prompt.md"
        assert (task_dir / task.system_prompt).exists()
        assert task.initial_state.json_db == "testcases/case_a/initial_state.json"
        assert (task_dir / task.initial_state.json_db).exists()
        assert task.grading == "testcases/case_a/grading.yaml"
        assert (task_dir / task.grading).exists()

    def test_flat_layout_end_to_end(self, tmp_path: Path) -> None:
        task_path = _make_flat_layout(tmp_path)
        task, task_dir = load_task_yaml(task_path)

        assert task.task_id == "flat"
        # Flat layout: task_dir is the task.yaml parent.
        assert task_dir == tmp_path / "flat_task"

    def test_validation_error_propagates(self, tmp_path: Path) -> None:
        bad = tmp_path / "task.yaml"
        bad.write_text("task_id: only_id\n")  # missing required fields
        with pytest.raises(ValidationError):
            load_task_yaml(bad)

    def test_non_mapping_yaml_raises_runtime_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "task.yaml"
        bad.write_text("- list\n- not\n- mapping\n")
        with pytest.raises(RuntimeError, match="not a YAML mapping"):
            load_task_yaml(bad)

    def test_non_string_compose_file_raises_with_task_path(self, tmp_path: Path) -> None:
        """A malformed ``environment_manifest.compose_file`` (e.g. an int
        or list) fails loud at load time with the task-file path in the
        message, instead of silently dropping the manifest and
        surfacing later as a confusing ``ProvisionError`` from the
        substrate layer."""
        task_path = tmp_path / "task.yaml"
        task_path.write_text(
            yaml.safe_dump(
                {
                    "task_id": "x",
                    "name": "x",
                    "category": "x",
                    "description": "x",
                    "initial_state": {},
                    "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                    "user_simulator": {"mode": "scripted", "scripted_flow": []},
                    "grading": "g.yaml",
                    "environment_manifest": {"compose_file": 42},
                }
            )
        )
        with pytest.raises(RuntimeError, match="compose_file.*must be a string"):
            load_task_yaml(task_path)

    def test_non_mapping_environment_manifest_raises_with_task_path(self, tmp_path: Path) -> None:
        """``environment_manifest`` present but not a mapping is a
        malformed task file; fail loud."""
        task_path = tmp_path / "task.yaml"
        task_path.write_text(
            yaml.safe_dump(
                {
                    "task_id": "x",
                    "name": "x",
                    "category": "x",
                    "description": "x",
                    "initial_state": {},
                    "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                    "user_simulator": {"mode": "scripted", "scripted_flow": []},
                    "grading": "g.yaml",
                    "environment_manifest": "not-a-mapping",
                }
            )
        )
        with pytest.raises(RuntimeError, match="environment_manifest.*YAML mapping"):
            load_task_yaml(task_path)

    def test_canonical_stack_compose_file_is_anchored(self, tmp_path: Path) -> None:
        """A task authored with the canonical ``stack.compose_file`` shape
        and a task-relative path gets anchored to an absolute path before
        ``TaskConfig`` is constructed — the invariant :func:`resolve`
        relies on for its "paths are absolute" contract."""
        env_fixture = (
            Path(__file__).parent.parent
            / "canonical"
            / "fixtures"
            / "environment_manifest"
            / "safe_one_service.yaml"
        )
        task_dir = tmp_path / "flat_task"
        task_dir.mkdir()
        rel_compose = task_dir / "environment.compose.yaml"
        rel_compose.write_text(env_fixture.read_text())
        task_path = task_dir / "task.yaml"
        task_path.write_text(
            yaml.safe_dump(
                {
                    "task_id": "x",
                    "name": "x",
                    "category": "x",
                    "description": "x",
                    "initial_state": {},
                    "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                    "user_simulator": {"mode": "scripted", "scripted_flow": []},
                    "grading": "g.yaml",
                    "environment_manifest": {
                        "stack": {"compose_file": "environment.compose.yaml"},
                    },
                }
            )
        )
        task, _ = load_task_yaml(task_path)
        assert task.environment_manifest is not None
        assert task.environment_manifest.stack is not None
        assert task.environment_manifest.stack.compose_file is not None
        assert task.environment_manifest.stack.compose_file.is_absolute()
        assert task.environment_manifest.stack.compose_file == rel_compose.resolve()

    def _write_task_with_stack(self, tmp_path: Path, stack: object) -> Path:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            {
                "task_id": "x",
                "name": "x",
                "category": "x",
                "description": "x",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "scripted", "scripted_flow": []},
                "grading": "g.yaml",
                "environment_manifest": {"stack": stack},
            },
        )
        return task_path

    def test_null_stack_warns_and_drops(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, None)
        with pytest.warns(DeprecationWarning, match=r"stack: null'.*is deprecated"):
            task, _ = load_task_yaml(task_path)
        # The null key is dropped so the loader treats it as unset (inherit-from-project).
        assert task.environment_manifest is not None
        assert task.environment_manifest.stack is None

    def test_null_stack_compose_file_warns_and_drops(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, {"compose_file": None})
        with pytest.warns(DeprecationWarning, match=r"stack\.compose_file: null'.*is deprecated"):
            task, _ = load_task_yaml(task_path)
        # The null compose_file key is dropped; stack subobject survives.
        assert task.environment_manifest is not None
        assert task.environment_manifest.stack is not None
        assert task.environment_manifest.stack.compose_file is None

    def test_empty_stack_loads(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, {})
        task, _ = load_task_yaml(task_path)
        assert task.environment_manifest is not None

    def test_stack_inputs_only_loads(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, {"inputs": {"x": "1"}})
        task, _ = load_task_yaml(task_path)
        assert task.environment_manifest is not None
        assert task.environment_manifest.stack is not None
        assert task.environment_manifest.stack.inputs == {"x": "1"}

    def test_stack_real_compose_file_loads(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, {"compose_file": "env.compose.yaml"})
        task, _ = load_task_yaml(task_path)
        assert task.environment_manifest is not None
        assert task.environment_manifest.stack is not None
        assert task.environment_manifest.stack.compose_file is not None

    def test_non_string_stack_compose_file_still_raises_type_error(self, tmp_path: Path) -> None:
        task_path = self._write_task_with_stack(tmp_path, {"compose_file": 3})
        with pytest.raises(
            RuntimeError, match="stack.compose_file' must be a string \\(got int\\)"
        ):
            load_task_yaml(task_path)

    def test_dangling_domain_ref_raises(self, tmp_path: Path) -> None:
        task_path = tmp_path / "testcases" / "c" / "task.yaml"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            yaml.safe_dump(
                {
                    "task_id": "x",
                    "domain": "../../_shared/missing.yaml",
                }
            )
        )
        with pytest.raises(RuntimeError, match="Domain file referenced"):
            load_task_yaml(task_path)


def test_inline_json_db_dict_survives_load(tmp_path: Path) -> None:
    """Inline ``json_db`` dict literals must round-trip without rewriting."""
    domain_path = tmp_path / "demo" / "_shared" / "domain.yaml"
    _write_yaml(
        domain_path,
        {
            "category": "tool_use",
            "tools": {"agent": {"enabled": ["t1"]}, "user": {"enabled": []}},
            "user_simulator": {"mode": "llm", "persona": "cooperative"},
        },
    )
    case_dir = tmp_path / "demo" / "testcases" / "c1"
    case_dir.mkdir(parents=True)
    _write_yaml(
        case_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            }
        },
    )
    _write_yaml(
        case_dir / "task.yaml",
        {
            "task_id": "inline",
            "name": "inline",
            "description": "inline",
            "domain": "../../_shared/domain.yaml",
            "initial_state": {"json_db": {"items": [{"id": 1}]}},
            "grading": "grading.yaml",
        },
    )
    task, _ = load_task_yaml(case_dir / "task.yaml")
    assert task.initial_state.json_db == {"items": [{"id": 1}]}


class TestSiblingGradingAutoPickup:
    """A ``grading.yaml`` next to ``task.yaml`` is auto-picked when the merged
    config sets no ``grading``. Explicit ``grading:`` always wins; a missing
    sibling leaves ``grading`` unset rather than fabricating a path — and what
    that unset resolves to is decided by the adapter the task declares."""

    @staticmethod
    def _write_minimal_task(task_dir: Path, **extra: object) -> Path:
        task_dir.mkdir(parents=True, exist_ok=True)
        data: dict = {"task_id": "min", "description": "minimal task"}
        data.update(extra)
        _write_yaml(task_dir / "task.yaml", data)
        return task_dir / "task.yaml"

    @staticmethod
    def _write_grading(path: Path) -> None:
        _write_yaml(
            path,
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0},
                    "pass_threshold": 1.0,
                }
            },
        )

    def test_sibling_grading_auto_picked_and_loads(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "flat_task"
        task_path = self._write_minimal_task(task_dir)
        self._write_grading(task_dir / "grading.yaml")

        task, task_root = load_task_yaml(task_path)

        expected = (task_dir / "grading.yaml").resolve()
        assert task.grading == str(expected)
        assert Path(task.grading).is_absolute()

        adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})
        grading = adapter.get_grading_config("min")
        assert grading.combine.weights == {"state_checks": 1.0}

    def test_no_sibling_leaves_grading_none(self, tmp_path: Path) -> None:
        task_path = self._write_minimal_task(tmp_path / "flat_task")
        task, _ = load_task_yaml(task_path)
        assert task.grading is None

    @pytest.mark.parametrize(
        ("declares_grading", "adapter_type", "kind"),
        [
            (True, "native", GradingSourceKind.ON_DISK),
            (True, "tau", GradingSourceKind.ON_DISK),
            (False, "native", GradingSourceKind.WITHHELD),
            (False, "tau", GradingSourceKind.UNINTERROGABLE),
        ],
        ids=[
            "a_declared_file_is_the_source",
            "a_declared_file_is_the_source_whatever_the_adapter",
            "the_adapter_that_grades_from_a_file_is_owed_one",
            "the_adapter_that_answers_for_itself_is_owed_nothing",
        ],
    )
    def test_the_grading_source_is_resolved_under_the_declared_adapter(
        self,
        tmp_path: Path,
        declares_grading: bool,
        adapter_type: str,
        kind: GradingSourceKind,
    ) -> None:
        """An absent grading source means opposite things to the two adapter kinds.

        ``get_grading_config`` is abstract and the implementations disagree: the
        native one refuses a task that names no file, while the terminal-bench one
        synthesises a whole config without reading the field. So the same absence is
        a defect under one declared adapter and unanswerable under another — while a
        *declared* path is the source under either, because the reading gate one
        layer down owns whether that file is there.
        """
        task_dir = tmp_path / "flat_task"
        extra: dict[str, object] = {"adapter_type": adapter_type}
        if declares_grading:
            extra["grading"] = "grading.yaml"
        task, resolved_dir = load_task_yaml(self._write_minimal_task(task_dir, **extra))

        source = grading_source_under_adapter(task, resolved_dir, task.adapter_type)

        assert source.kind is kind
        if kind is GradingSourceKind.ON_DISK:
            assert source.path == task_dir / "grading.yaml"
            assert source.reason == ""
        else:
            assert source.path is None

    def test_a_withheld_source_names_the_task_and_both_ways_to_supply_one(
        self, tmp_path: Path
    ) -> None:
        """The sentence is what an author reads instead of a leaked ``TypeError``.

        It has to carry the task and both fixes, because no caller supplies them both:
        the CLI's line names the file rather than the task, and neither it nor the
        pre-run gate's aggregate repeats a fix.
        """
        task_path = self._write_minimal_task(tmp_path / "flat_task")
        task, task_dir = load_task_yaml(task_path)

        reason = grading_source_under_adapter(task, task_dir, task.adapter_type).reason

        assert "'min'" in reason
        assert "`grading:`" in reason
        assert "grading.yaml beside its task.yaml" in reason
        assert "before any trial is scheduled" in reason

    def test_an_uninterrogable_source_names_the_adapter_it_could_not_ask(
        self, tmp_path: Path
    ) -> None:
        """The skip reason travels a channel that never fails a pack, so it says
        which declared adapter made the absence unanswerable rather than asserting
        a defect."""
        task_path = self._write_minimal_task(tmp_path / "flat_task", adapter_type="tau")
        task, task_dir = load_task_yaml(task_path)

        reason = grading_source_under_adapter(task, task_dir, task.adapter_type).reason

        assert "'tau'" in reason
        assert "not checkable" in reason

    @pytest.mark.parametrize(
        ("kind", "path", "reason"),
        [
            (GradingSourceKind.ON_DISK, None, ""),
            (GradingSourceKind.ON_DISK, Path("grading.yaml"), "an absence"),
            (GradingSourceKind.WITHHELD, Path("grading.yaml"), "an absence"),
            (GradingSourceKind.WITHHELD, None, ""),
        ],
        ids=[
            "an_on_disk_source_with_no_file",
            "an_on_disk_source_explaining_itself",
            "an_absence_carrying_a_file",
            "an_absence_explaining_nothing",
        ],
    )
    def test_a_source_that_contradicts_its_own_kind_is_refused(
        self, kind: GradingSourceKind, path: Path | None, reason: str
    ) -> None:
        """A source is exactly one of a file and a sentence.

        Both halves are load-bearing: a consumer reads the path on the one kind and
        prints the sentence on the other two, so a value carrying neither would be
        graded against nothing and a value carrying both would refuse a task it had
        already resolved a file for.
        """
        with pytest.raises(ValueError, match="grading source of kind"):
            GradingSource(kind=kind, path=path, reason=reason)

    def test_explicit_grading_not_overridden_by_sibling(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "flat_task"
        task_path = self._write_minimal_task(task_dir, grading="other.yaml")
        self._write_grading(task_dir / "grading.yaml")

        task, _ = load_task_yaml(task_path)
        assert task.grading == "other.yaml"


def test_load_task_yaml_real_example(tmp_path: Path) -> None:
    """Smoke check: the in-tree ``examples/native_shared_domain`` loads."""
    repo_root = Path(__file__).resolve().parents[2]
    case = (
        repo_root
        / "examples"
        / "native"
        / "native_shared_domain"
        / "dataset"
        / "notes"
        / "testcases"
        / "add_first_note"
        / "task.yaml"
    )
    if not case.exists():
        pytest.skip(f"example not found at {case}")
    task, task_dir = load_task_yaml(case)
    assert task.task_id == "notes_add_first_note"
    # Effective task dir is <…>/dataset/notes (the domain root).
    assert task_dir.name == "notes"
    # Every relative-path field resolves cleanly from task_dir.
    assert (task_dir / task.tools.agent["mcp_server"]).exists()
    assert task.system_prompt is not None
    assert (task_dir / task.system_prompt).exists()
    json_db_ref = task.initial_state.json_db
    assert isinstance(json_db_ref, str)
    initial_state = json.loads((task_dir / json_db_ref).read_text())
    assert "notes" in initial_state
    assert (task_dir / task.grading).exists()
