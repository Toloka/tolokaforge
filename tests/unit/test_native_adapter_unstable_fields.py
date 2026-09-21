"""Native adapter carries pack-authored ``fixtures/unstable_fields.json``
through to :class:`RunnerInitialStateConfig.unstable_fields`.

The bundle-writer output convention puts unstable-field declarations at
``fixtures/unstable_fields.json`` beside the task's ``task.yaml``. Before
this file's fixes shipped, the native adapter built
``RunnerInitialStateConfig(unstable_fields=[])`` unconditionally — the
pack signal was authored and silently dropped, and the runner + core
substrates hashed the state with no mask, false-failing on
auto-generated ids and timestamps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _write_task_pack(
    root: Path,
    *,
    unstable_fields: list[dict] | None,
) -> Path:
    task_dir = root / "tasks" / "responses"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text(json.dumps({"responses": []}))
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "responses",
                "name": "Unstable-fields fixture",
                "category": "test",
                "description": "adapter carries unstable_fields.json",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {
                    "mode": "scripted",
                    "scripted_flow": [{"role": "user", "content": "hi"}],
                },
                "grading": "grading.yaml",
            }
        )
    )
    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0, "transcript_rules": 0.0},
                    "pass_threshold": 0.7,
                },
                "state_checks": {"jsonpaths": []},
                "transcript_rules": {"max_turns": 5, "disallow_regex": []},
                "llm_judge": None,
            }
        )
    )
    if unstable_fields is not None:
        (task_dir / "fixtures").mkdir()
        (task_dir / "fixtures" / "unstable_fields.json").write_text(json.dumps(unstable_fields))
    return root


def _adapter(root: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "tasks/**/task.yaml"})


def test_missing_fixtures_dir_leaves_unstable_fields_empty(tmp_path: Path) -> None:
    _write_task_pack(tmp_path, unstable_fields=None)
    td = _adapter(tmp_path).to_task_description("responses")
    assert td.initial_state.unstable_fields == []


def test_authored_specs_reach_initial_state(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        unstable_fields=[
            {"table_name": "responses", "field_name": "response_id", "reason": "auto_id"},
            {"table_name": "tickets", "field_name": "updated_at", "reason": "timestamp"},
        ],
    )
    td = _adapter(tmp_path).to_task_description("responses")
    specs = td.initial_state.unstable_fields
    assert [(s.table_name, s.field_name, s.reason) for s in specs] == [
        ("responses", "response_id", "auto_id"),
        ("tickets", "updated_at", "timestamp"),
    ]


def test_malformed_file_refuses_loudly(tmp_path: Path) -> None:
    _write_task_pack(tmp_path, unstable_fields=None)
    fixtures = tmp_path / "tasks" / "responses" / "fixtures"
    fixtures.mkdir()
    (fixtures / "unstable_fields.json").write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="expected a JSON list"):
        _adapter(tmp_path).to_task_description("responses")


def test_real_pack_shape_response_id_fold(tmp_path: Path) -> None:
    """A pack declaring ``response_id`` on the ``*_email_responses`` table (the
    shape cargo tasks ``G-DSC-023`` / ``G-DSC-024`` / ``P-BCN-022`` already
    author) causes both grading substrates to collapse state pairs that differ
    only on the surrogate id to the same hash.

    Trial state and golden state come out of two runs of the same golden
    replay; the surrogate id is generated per run so it always differs. Before
    this fix the adapter dropped the fixture and both substrates hashed the id
    into the digest, false-failing on every trial.
    """
    from tolokaforge.core.grading.state_checks import load_task_unstable_fields, state_digest
    from tolokaforge.core.hash import compute_stable_hash

    _write_task_pack(
        tmp_path,
        unstable_fields=[
            {
                "table_name": "ots_airline_cargo_d365_email_responses",
                "field_name": "response_id",
                "reason": "auto_id",
            },
        ],
    )
    task_dir = tmp_path / "tasks" / "responses"
    mask = load_task_unstable_fields(task_dir)
    assert mask == ["ots_airline_cargo_d365_email_responses.response_id"]

    trial_state = {
        "ots_airline_cargo_d365_email_responses": [
            {"response_id": "auto-2026-09-21-A", "case_id": "CA-1", "sent_at": "T0"},
        ]
    }
    golden_state = {
        "ots_airline_cargo_d365_email_responses": [
            {"response_id": "auto-2026-09-21-B", "case_id": "CA-1", "sent_at": "T0"},
        ]
    }
    # Pre-fix: no mask, digests diverge on both substrates.
    assert state_digest(trial_state) != state_digest(golden_state)
    assert compute_stable_hash(trial_state) != compute_stable_hash(golden_state)
    # Post-fix: mask applied, digests match on both substrates.
    assert state_digest(trial_state, unstable_fields=mask) == state_digest(
        golden_state, unstable_fields=mask
    )
    assert compute_stable_hash(trial_state, mask) == compute_stable_hash(golden_state, mask)
