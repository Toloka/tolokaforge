from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.adapters.native_verify import normalize_native_tool, verify_native_tasks

pytestmark = pytest.mark.unit


def _example_domain() -> Path:
    return (
        Path(__file__).parents[2]
        / "examples"
        / "native"
        / "native_shared_domain"
        / "dataset"
        / "notes"
    )


def test_native_adapter_wires_schema_and_mask_references() -> None:
    domain = _example_domain()
    adapter = NativeAdapter(
        {
            "base_dir": str(domain),
            "tasks_glob": "testcases/add_first_note/task.yaml",
        }
    )

    description = adapter.to_task_description("notes_add_first_note")

    assert description.initial_state.schemas[0].table_name == "notes"
    assert description.initial_state.schemas[0].primary_key == "id"
    assert description.initial_state.unstable_fields == []
    assert not any(
        path.startswith("testcases/recall_existing_note/") for path in description.tool_artifacts
    )


def test_native_verify_executes_live_server_and_deterministic_replay(tmp_path: Path) -> None:
    domain = _example_domain()
    report = verify_native_tasks(str(domain / "testcases" / "*" / "task.yaml"))

    assert report.passed
    assert len(report.cases) == 4
    assert all(case.passed for case in report.cases)


def test_native_verify_rejects_stale_tool_fixture(tmp_path: Path) -> None:
    domain = tmp_path / "notes"
    shutil.copytree(_example_domain(), domain)
    fixture_path = domain / "fixtures" / "tools.json"
    fixture = json.loads(fixture_path.read_text())
    fixture[0]["description"] = "stale"
    fixture_path.write_text(json.dumps(fixture))

    report = verify_native_tasks(str(domain / "testcases" / "add_first_note" / "task.yaml"))

    assert not report.passed
    failed = [check for check in report.cases[0].checks if not check.passed]
    assert any("does not exactly match" in check.detail for check in failed)


def test_native_tool_normalizer_is_the_public_fixture_authority() -> None:
    assert normalize_native_tool(
        {
            "name": "lookup",
            "description": "Look up a record.",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object"},
        }
    ) == {
        "name": "lookup",
        "description": "Look up a record.",
        "parameters": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object"},
    }


def test_native_verify_rejects_fixture_only_output_schema(tmp_path: Path) -> None:
    domain = tmp_path / "notes"
    shutil.copytree(_example_domain(), domain)
    fixture_path = domain / "fixtures" / "tools.json"
    fixture = json.loads(fixture_path.read_text())
    fixture[0]["outputSchema"] = {"type": "integer"}
    fixture_path.write_text(json.dumps(fixture))

    report = verify_native_tasks(str(domain / "testcases" / "add_first_note" / "task.yaml"))

    assert not report.passed
    failed = [check for check in report.cases[0].checks if not check.passed]
    assert any("does not exactly match" in check.detail for check in failed)


def test_native_verify_rejects_duplicate_primary_keys(tmp_path: Path) -> None:
    domain = tmp_path / "notes"
    shutil.copytree(_example_domain(), domain)
    state_path = domain / "testcases" / "add_first_note" / "initial_state.json"
    state = json.loads(state_path.read_text())
    state["notes"] = [
        {"id": "n1", "title": "one", "body": "first"},
        {"id": "n1", "title": "two", "body": "second"},
    ]
    state_path.write_text(json.dumps(state))

    report = verify_native_tasks(str(domain / "testcases" / "add_first_note" / "task.yaml"))

    assert not report.passed
    failed = [check for check in report.cases[0].checks if not check.passed]
    assert any("unique and non-null" in check.detail for check in failed)
