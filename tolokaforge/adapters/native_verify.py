"""Executable verification for native TolokaForge task trees."""

from __future__ import annotations

import glob
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, computed_field

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.adapters.native import _load_json_list_reference
from tolokaforge.adapters.native_active_bundle import materialize_active_native_case
from tolokaforge.adapters.native_consumer_checks import (
    GradingComponentSurvival,
    check_consumer_surfaces,
)
from tolokaforge.runner.models import InitialStateConfig as RunnerInitialStateConfig
from tolokaforge.runner.tool_factory import MCPServerProcess
from tolokaforge.runner.tool_result import tool_error_message


class NativeVerificationCheck(BaseModel):
    """One content-bound native verification result."""

    name: str
    passed: bool
    detail: str = ""


class NativeCaseVerification(BaseModel):
    """Verification results for one native testcase."""

    task_id: str
    task_file: str
    case_digest: str
    checks: list[NativeVerificationCheck] = Field(default_factory=list)
    #: Per-component active-weight survival findings (present/dropped/why),
    #: populated by the consumer-surface pass. Additive: absent/empty for
    #: cases that failed before the consumer surfaces could run.
    grading_components: list[GradingComponentSurvival] = Field(default_factory=list)

    @computed_field
    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


class NativeVerificationReport(BaseModel):
    """Fail-closed report emitted by :func:`verify_native_tasks`."""

    schema_version: int = 1
    tasks_pattern: str
    domain_digests: dict[str, str] = Field(default_factory=dict)
    cases: list[NativeCaseVerification] = Field(default_factory=list)
    duplicate_task_ids: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def passed(self) -> bool:
        return (
            bool(self.cases)
            and not self.duplicate_task_ids
            and all(case.passed for case in self.cases)
        )


def verify_native_tasks(tasks_pattern: str) -> NativeVerificationReport:
    """Verify every native task matched by *tasks_pattern* using its real MCP server."""
    task_files = sorted(Path(path).resolve() for path in glob.glob(tasks_pattern, recursive=True))
    report = NativeVerificationReport(tasks_pattern=tasks_pattern)
    seen_ids: set[str] = set()

    for task_file in task_files:
        case = _verify_case(task_file)
        if case.task_id in seen_ids:
            report.duplicate_task_ids.append(case.task_id)
        seen_ids.add(case.task_id)
        report.cases.append(case)

        try:
            _, task_root = load_task_yaml(task_file)
        except Exception:
            continue
        report.domain_digests.setdefault(str(task_root), _tree_digest(task_root))

    report.duplicate_task_ids = sorted(set(report.duplicate_task_ids))
    return report


def write_native_verification_report(report: NativeVerificationReport, path: Path) -> None:
    """Write a deterministic JSON verification report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verify_case(task_file: Path) -> NativeCaseVerification:
    try:
        task, task_root = load_task_yaml(task_file)
    except Exception as exc:
        return NativeCaseVerification(
            task_id=f"invalid:{task_file}",
            task_file=str(task_file),
            case_digest=_file_digest(task_file),
            checks=[_failed("task_load", exc)],
        )

    case = NativeCaseVerification(
        task_id=task.task_id,
        task_file=str(task_file),
        case_digest=_tree_digest(task_file.parent),
    )

    try:
        initial_tables = _load_initial_tables(task.initial_state.json_db, task_root)
        schemas = _load_json_list_reference(
            task.initial_state.schemas,
            task_dir=task_root,
            field_name="schemas",
        )
        unstable_fields = _load_json_list_reference(
            task.initial_state.unstable_fields,
            task_dir=task_root,
            field_name="unstable_fields",
        )
        runner_state = RunnerInitialStateConfig(
            tables=initial_tables,
            schemas=schemas,
            unstable_fields=unstable_fields,
        )
        _validate_primary_keys(runner_state)
        case.checks.append(_passed("initial_state", "state, schemas, and masks are valid"))
    except Exception as exc:
        case.checks.append(_failed("initial_state", exc))
        return case

    mcp_ref = task.tools.agent.get("mcp_server")
    if not isinstance(mcp_ref, str) or not mcp_ref:
        case.checks.append(_failed("mcp_server", "agent MCP server is required"))
        return case

    try:
        with tempfile.TemporaryDirectory(prefix="tolokaforge-native-verify-") as temp_dir:
            server_path = _materialize_active_case(
                task_file=task_file,
                destination=Path(temp_dir),
            )
            process = MCPServerProcess(script_path=str(server_path))
            try:
                process.start()
                live_tools = _live_tools(process)
                case.checks.append(_passed("mcp_start", f"listed {len(live_tools)} tools"))
                _verify_tool_fixture(task_root, live_tools)
                enabled = task.tools.agent.get("enabled", [])
                missing = sorted(set(enabled) - {tool["name"] for tool in live_tools})
                if missing:
                    raise RuntimeError(f"enabled tools missing from live MCP schema: {missing}")
                case.checks.append(
                    _passed("tool_schemas", "fixture exactly matches live tools/list")
                )

                golden_actions = _load_golden_actions(task_root / task.grading)
                first = _replay(process, initial_tables, golden_actions)
                second = _replay(process, initial_tables, golden_actions)
                if first != second:
                    raise RuntimeError("golden replay is nondeterministic across identical resets")
                case.checks.append(
                    _passed(
                        "golden_replay", f"deterministic replay of {len(golden_actions)} actions"
                    )
                )
            finally:
                process.stop()
    except Exception as exc:
        check_name = "mcp_runtime"
        if any(check.name == "mcp_start" for check in case.checks):
            check_name = "tool_schemas_or_golden"
        case.checks.append(_failed(check_name, exc))

    _append_consumer_surface_checks(case, task_file)
    return case


def _append_consumer_surface_checks(case: NativeCaseVerification, task_file: Path) -> None:
    """Exercise every real consumer surface and record structured findings.

    A golden replay that succeeds proves the MCP machinery, not the grading
    contract: trial registration (``to_task_description``), artifact
    persistence (core ``get_grading_config``), and the adapter load path each
    parse the same YAML with different schemas — the D16 class of defect only
    exists at those boundaries. All findings land as ordinary report checks;
    the helper itself never raises.
    """
    report = check_consumer_surfaces(task_file)
    for surface in report.surfaces:
        case.checks.append(
            NativeVerificationCheck(
                name=f"consumer_{surface.surface}",
                passed=surface.passed,
                detail=surface.detail,
            )
        )
    case.grading_components = report.components
    dropped = report.dropped_components()
    detail = "; ".join(
        f"{component.component} (weight={component.weight:g}, "
        f"{'present' if component.present else 'dropped'}): {component.reason}"
        for component in report.components
    )
    case.checks.append(
        NativeVerificationCheck(
            name="grading_component_survival",
            passed=not dropped,
            detail=detail or "no positively weighted grading components declared",
        )
    )


def _materialize_active_case(
    *,
    task_file: Path,
    destination: Path,
) -> Path:
    return materialize_active_native_case(task_file, destination).server_path


def _load_initial_tables(
    value: str | dict[str, Any] | None,
    task_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    if value is None:
        return {}
    payload: Any = value
    if isinstance(value, str):
        path = task_root / value
        if not path.is_file():
            raise RuntimeError(f"initial_state.json_db file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("initial_state.json_db must resolve to a JSON object")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for table, records in payload.items():
        if not isinstance(table, str) or not isinstance(records, list):
            raise RuntimeError("initial state must map table names to record lists")
        if any(not isinstance(record, dict) for record in records):
            raise RuntimeError(f"initial-state table {table!r} contains a non-object record")
        normalized[table] = records
    return normalized


def _validate_primary_keys(state: RunnerInitialStateConfig) -> None:
    for schema in state.schemas:
        records = state.tables.get(schema.table_name)
        if records is None:
            raise RuntimeError(f"schema references missing table {schema.table_name!r}")
        observed: set[Any] = set()
        for index, record in enumerate(records):
            if schema.primary_key not in record:
                raise RuntimeError(
                    f"{schema.table_name}[{index}] lacks primary key {schema.primary_key!r}"
                )
            value = record[schema.primary_key]
            try:
                duplicate = value in observed
                observed.add(value)
            except TypeError as exc:
                raise RuntimeError(
                    f"{schema.table_name}[{index}] primary key is not hashable"
                ) from exc
            if value is None or duplicate:
                raise RuntimeError(
                    f"{schema.table_name} primary key {schema.primary_key!r} must be "
                    f"unique and non-null; invalid value at index {index}: {value!r}"
                )


def _live_tools(process: MCPServerProcess) -> list[dict[str, Any]]:
    result = process.send_request("tools/list", {})
    tools = result.get("tools")
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise RuntimeError("MCP tools/list did not return a tool list")
    return sorted((normalize_native_tool(tool) for tool in tools), key=lambda tool: tool["name"])


def normalize_native_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Return a lossless canonical fixture representation of one MCP tool.

    MCP calls the input schema ``inputSchema`` while historical TolokaForge
    fixtures call it ``parameters``.  That spelling difference is the only
    normalization performed: output schemas, annotations, and any future MCP
    schema fields remain equality-significant.
    """
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("tool schema lacks a non-empty name")
    if "parameters" in tool and "inputSchema" in tool:
        raise RuntimeError(f"tool {name!r} declares both parameters and inputSchema")
    parameters = tool.get("parameters", tool.get("inputSchema"))
    if not isinstance(parameters, dict):
        raise RuntimeError(f"tool {name!r} lacks an object input schema")
    normalized = dict(tool)
    normalized.pop("inputSchema", None)
    normalized["parameters"] = parameters
    normalized.setdefault("description", "")
    return normalized


def _verify_tool_fixture(task_root: Path, live_tools: list[dict[str, Any]]) -> None:
    fixture_path = task_root / "fixtures" / "tools.json"
    if not fixture_path.is_file():
        raise RuntimeError(f"live-derived tool fixture is missing: {fixture_path}")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, list) or any(not isinstance(tool, dict) for tool in fixture):
        raise RuntimeError(f"tool fixture must be a JSON list of objects: {fixture_path}")
    normalized = sorted(
        (normalize_native_tool(tool) for tool in fixture), key=lambda tool: tool["name"]
    )
    if normalized != live_tools:
        raise RuntimeError("fixtures/tools.json does not exactly match live MCP tools/list")


def _load_golden_actions(grading_path: Path) -> list[dict[str, Any]]:
    if not grading_path.is_file():
        raise RuntimeError(f"grading file not found: {grading_path}")
    grading = yaml.safe_load(grading_path.read_text(encoding="utf-8")) or {}
    actions = grading.get("state_checks", {}).get("hash", {}).get("golden_actions", [])
    if not isinstance(actions, list):
        raise RuntimeError("state_checks.hash.golden_actions must be a list")
    return actions


def _replay(
    process: MCPServerProcess,
    initial_tables: dict[str, list[dict[str, Any]]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    reset = process.send_request(
        "tools/call",
        {
            "name": "_tolokaforge_set_state_",
            "arguments": {"state_json": json.dumps(initial_tables)},
        },
    )
    reset_error = tool_error_message(reset)
    if reset_error is not None:
        raise RuntimeError(f"golden replay reset returned an error: {reset_error}")
    if process.get_state() != initial_tables:
        raise RuntimeError("golden replay reset did not restore the exact initial state")
    outputs: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise RuntimeError(f"golden action {index} is not an object")
        name = action.get("name")
        arguments = action.get("kwargs", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RuntimeError(f"golden action {index} has invalid name or kwargs")
        result = process.send_request("tools/call", {"name": name, "arguments": arguments})
        error = tool_error_message(result)
        if error is not None:
            raise RuntimeError(f"golden action {index} ({name}) returned an error: {error}")
        outputs.append(result)
    return {"outputs": outputs, "state": process.get_state()}


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def _passed(name: str, detail: str) -> NativeVerificationCheck:
    return NativeVerificationCheck(name=name, passed=True, detail=detail)


def _failed(name: str, error: object) -> NativeVerificationCheck:
    return NativeVerificationCheck(name=name, passed=False, detail=str(error))
