"""A task declares ``tools.<actor>.<name>.mutates_state`` where the tool's author is silent.

Locks the task-side half of ADR-0061's declaration: the three forms reach the
emitted :class:`~tolokaforge.runner.models.ToolSchema`; a declaration over the
author's own is refused unless it is ``true``; a ``read_when`` path the tool's
schema rules out is refused; and a task file's block for a tool it does not enable
is refused whatever it carries, while a project ``task_defaults`` block stays inert.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters._task_loader import (
    ToolActor,
    load_task_yaml,
    tool_mutates_state_declarations,
)
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.models import MutatesStateRule
from tolokaforge.tools.builtin.http_request import safe_methods_read

pytestmark = pytest.mark.unit

_GET_ONLY = {"read_when": {"args": {"method": {"in_": ["GET", "HEAD"]}}}}


def _pack(
    root: Path,
    agent_block: dict[str, Any],
    *,
    mcp_cache: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a one-task native pack under *root* and return the ``task.yaml``."""
    task_dir = root / "decl_task"
    task_dir.mkdir(parents=True)
    if mcp_cache is not None:
        (task_dir / "mcp_server.py").write_text("")
        (task_dir / "fixtures").mkdir()
        (task_dir / "fixtures" / "tools.json").write_text(json.dumps(mcp_cache))
        agent_block = {"mcp_server": "mcp_server.py", **agent_block}
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text(
        yaml.safe_dump(
            {
                "task_id": "decl_task",
                "name": "declaration task",
                "description": "tools declaring mutates_state",
                "category": "compute",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "go",
                "initial_state": {},
                "tools": {"agent": agent_block, "user": {"enabled": []}},
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    return task_yaml


def _emitted(root: Path) -> dict[str, Any]:
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(root)})
    description = adapter.to_task_description("decl_task")
    return {tool.name: tool.mutates_state for tool in description.agent_tools}


def _cached(name: str, mutates_state: bool | dict[str, Any] | None) -> dict[str, Any]:
    return {
        "name": name,
        "description": name,
        "parameters": {
            "type": "object",
            "properties": {"method": {"type": "string"}},
            "additionalProperties": False,
        },
        "mutates_state": mutates_state,
    }


_LISTING_READS = {"read_when": {"args": {"command": {"regex": "^ls( |$)"}}}}


@pytest.mark.parametrize(
    "declared",
    [False, True, _LISTING_READS],
    ids=["false", "true", "read-rule"],
)
def test_a_task_declaration_reaches_the_tool_schema_where_the_author_is_silent(
    tmp_path: Path, declared: bool | dict[str, Any]
) -> None:
    """``bash`` is a ``compute`` builtin whose policy declares nothing."""
    _pack(tmp_path, {"enabled": ["bash"], "bash": {"mutates_state": declared}})

    emitted = _emitted(tmp_path)["bash"]

    expected = MutatesStateRule.model_validate(declared) if isinstance(declared, dict) else declared
    assert emitted == expected


def test_the_declaration_is_no_tool_init_kwarg(tmp_path: Path) -> None:
    _pack(tmp_path, {"enabled": ["http_request"], "http_request": {"mutates_state": True}})
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})

    (tool,) = adapter.to_task_description("decl_task").agent_tools

    assert tool.tool_config == {}


@pytest.mark.parametrize(
    ("author", "declared", "accepted"),
    [
        (False, True, True),
        (None, _GET_ONLY, True),
        (False, False, False),
        (True, False, False),
        (False, _GET_ONLY, False),
        (_GET_ONLY, True, True),
        (_GET_ONLY, False, False),
        (_GET_ONLY, _GET_ONLY, False),
    ],
    ids=[
        "true-tightens",
        "author-silent",
        "false-over-false",
        "false-over-true",
        "rule-over-false",
        "true-over-an-author-rule",
        "false-over-an-author-rule",
        "rule-over-an-author-rule",
    ],
)
def test_only_true_is_declared_over_an_mcp_authors_declaration(
    tmp_path: Path,
    author: bool | dict[str, Any] | None,
    declared: bool | dict[str, Any],
    accepted: bool,
) -> None:
    _pack(
        tmp_path,
        {"enabled": ["rest_call"], "rest_call": {"mutates_state": declared}},
        mcp_cache=[_cached("rest_call", author)],
    )

    if accepted:
        emitted = _emitted(tmp_path)["rest_call"]
        assert emitted == (
            MutatesStateRule.model_validate(declared) if isinstance(declared, dict) else declared
        )
        return
    with pytest.raises(ValueError, match="overrides the tool author's own declaration") as caught:
        _emitted(tmp_path)
    assert "tools.agent.rest_call.mutates_state in task 'decl_task'" in str(caught.value)


def test_an_mcp_authors_rule_reaches_the_tool_schema(tmp_path: Path) -> None:
    _pack(tmp_path, {"enabled": ["rest_call"]}, mcp_cache=[_cached("rest_call", _GET_ONLY)])

    assert _emitted(tmp_path)["rest_call"] == MutatesStateRule.model_validate(_GET_ONLY)


@pytest.mark.parametrize(
    ("author", "error"),
    [
        ("false", "mutates_state is 'false'"),
        ({"read_when": {"args": {"method": {"in": ["GET"]}}}}, "not a read rule"),
        (
            {"read_when": {"args": {"verb": {"equals": "GET"}}}},
            "the rest_call tool's own mutates_state.read_when.args.verb",
        ),
    ],
    ids=["a-string", "an-unknown-operator", "a-path-the-closed-schema-rules-out"],
)
def test_a_malformed_author_declaration_in_the_cache_is_refused(
    tmp_path: Path, author: Any, error: str
) -> None:
    """The cache is the author's word on the wire; a rule it cannot read would hide
    writes from the judge, so the description refuses it, naming the tool."""
    _pack(tmp_path, {"enabled": ["rest_call"]}, mcp_cache=[_cached("rest_call", author)])

    with pytest.raises(ValueError, match="rest_call") as caught:
        _emitted(tmp_path)
    assert error in str(caught.value)


@pytest.mark.parametrize(
    ("tool_name", "declared", "authors"),
    [
        ("read_file", _GET_ONLY, "false"),
        ("http_request", False, "a read_when rule"),
        ("http_request", _GET_ONLY, "a read_when rule"),
    ],
    ids=["rule-over-a-read-category", "false-over-http-request", "rule-over-http-request"],
)
def test_a_declaration_over_a_builtins_own_is_refused(
    tmp_path: Path, tool_name: str, declared: bool | dict[str, Any], authors: str
) -> None:
    """``read_file`` is a ``read`` builtin, so its author declares ``false``;
    ``http_request``'s policy declares its safe methods as reads."""
    _pack(tmp_path, {"enabled": [tool_name], tool_name: {"mutates_state": declared}})

    with pytest.raises(ValueError, match=rf"author's own declaration \({authors}\)"):
        _emitted(tmp_path)


def test_http_request_carries_its_policys_rule_unless_the_task_declares_true(
    tmp_path: Path,
) -> None:
    _pack(tmp_path / "silent", {"enabled": ["http_request"]})
    _pack(
        tmp_path / "tightened",
        {"enabled": ["http_request"], "http_request": {"mutates_state": True}},
    )

    assert _emitted(tmp_path / "silent")["http_request"] == MutatesStateRule.model_validate(
        safe_methods_read()
    )
    assert _emitted(tmp_path / "tightened")["http_request"] is True


def test_a_rule_over_the_search_tools_declaration_is_refused(tmp_path: Path) -> None:
    _pack(tmp_path, {"enabled": ["search_kb"], "search_kb": {"mutates_state": _GET_ONLY}})

    with pytest.raises(ValueError, match="tools.agent.search_kb.mutates_state"):
        _emitted(tmp_path)


def test_a_path_a_closed_schema_does_not_declare_is_refused(tmp_path: Path) -> None:
    """``bash`` admits no argument but ``command``, so a rule on ``method`` reads no call."""
    rule = {"read_when": {"args": {"method": {"equals": "GET"}}}}
    _pack(tmp_path, {"enabled": ["bash"], "bash": {"mutates_state": rule}})

    with pytest.raises(ValueError, match="argument 'method' is not one 'bash' declares"):
        _emitted(tmp_path)


def test_a_path_an_open_schema_does_not_declare_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    open_schema = {
        **_cached("rest_call", None),
        "parameters": {"type": "object", "properties": {"method": {"type": "string"}}},
    }
    rule = {"read_when": {"args": {"verb": {"equals": "GET"}}}}
    _pack(
        tmp_path,
        {"enabled": ["rest_call"], "rest_call": {"mutates_state": rule}},
        mcp_cache=[open_schema],
    )

    with caplog.at_level(logging.WARNING):
        emitted = _emitted(tmp_path)["rest_call"]

    assert isinstance(emitted, MutatesStateRule)
    assert any("argument 'verb' is not one" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("false", "is 'false'; declare true, false"),
        (None, "is null; declare false, true"),
        ({"read_when": {"args": {}}}, "not a read rule"),
    ],
    ids=["string", "null", "no-paths"],
)
def test_a_malformed_declaration_is_refused(tmp_path: Path, value: Any, error: str) -> None:
    task_yaml = _pack(
        tmp_path, {"enabled": ["http_request"], "http_request": {"mutates_state": value}}
    )
    task, _ = load_task_yaml(task_yaml)

    with pytest.raises(ValueError, match=error):
        tool_mutates_state_declarations(task, ToolActor.AGENT)


@pytest.mark.parametrize(
    "block",
    [{"output_max_chars": 500}, {"allowed_hosts": ["example.com"]}, {"mutates_state": True}],
    ids=["output-cap", "init-kwarg", "mutates-state"],
)
def test_a_block_for_a_tool_the_task_does_not_enable_is_refused(
    tmp_path: Path, block: dict[str, Any]
) -> None:
    task_yaml = _pack(tmp_path, {"enabled": ["read_file"], "http_reqest": block})

    with pytest.raises(ValueError, match=r"tools\.agent configures \['http_reqest'\]") as caught:
        load_task_yaml(task_yaml)
    assert "it lists ['read_file']" in str(caught.value)


def test_a_project_block_for_a_tool_the_task_does_not_enable_is_inert(tmp_path: Path) -> None:
    task_yaml = _pack(tmp_path, {"enabled": ["read_file"]})
    project_defaults = {"tools": {"agent": {"bash": {"mutates_state": True}}}}

    task, _ = load_task_yaml(task_yaml, project_task_defaults=project_defaults)

    assert tool_mutates_state_declarations(task, ToolActor.AGENT) == {}


_REPO = Path(__file__).resolve().parents[3]


def _authored_task_files() -> list[Path]:
    """Every in-repo ``task.yaml`` an author wrote: recorded trial bundles excluded."""
    return sorted(
        path
        for root in (_REPO / "tests" / "data", _REPO / "examples")
        for path in root.rglob("task.yaml")
        if not (path.parent / "trajectory.yaml").exists()
    )


def test_no_in_repo_task_file_writes_a_block_for_a_tool_it_does_not_enable() -> None:
    """Each file is loaded under its enclosing project, as ``tolokaforge validate``
    loads it. A file that fails for any other reason is not this refusal's concern."""
    from tolokaforge.dx.cli.main import _load_task_under_its_project

    refused: list[str] = []
    loaded = 0
    for task_file in _authored_task_files():
        try:
            _load_task_under_its_project(task_file)
        except Exception as exc:
            if "enabled does not list" in str(exc):
                refused.append(f"{task_file.relative_to(_REPO)}: {exc}")
            continue
        loaded += 1

    assert refused == []
    assert loaded > 100
