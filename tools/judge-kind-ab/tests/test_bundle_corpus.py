"""Unit-tier lock for :mod:`judge_kind_ab.bundle_corpus`.

Builds synthetic bundle directories with
:func:`tolokaforge.core.grading.bundle.serialize_grade_bundle` and asserts the
reconstruction round-trips correctly and fails loud on the two silently-
defaultable inputs the live-A/B corpus must never paper over.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from judge_kind_ab.bundle_corpus import corpus_entry_from_bundle, load_corpus_from_run

from tolokaforge.core.grading.bundle import serialize_grade_bundle

pytestmark = pytest.mark.unit

_RUBRIC = {
    "criteria": [
        {"id": "helped", "description": "Agent resolved the user's request.", "kind": "binary"},
    ],
}

_TASK_DESCRIPTION: dict[str, Any] = {
    "task_id": "corpus-task-1",
    "name": "corpus task",
    "category": "airline",
    "description": "Help the user.",
    "adapter_type": "native",
    "system_prompt": "You are a helpful airline agent.",
}

_TRAJECTORY: dict[str, Any] = {
    "task_id": "corpus-task-1",
    "trial_index": 0,
    "start_ts": "2026-09-10T00:00:00+00:00",
    "end_ts": "2026-09-10T00:01:00+00:00",
    "messages": [
        {"role": "user", "content": "Can you help me rebook my flight?"},
        {"role": "assistant", "content": "Sure, let me look into that."},
    ],
}


def _grading_config(*, with_llm_judge: bool = True) -> dict[str, Any]:
    if not with_llm_judge:
        return {}
    return {"llm_judge": {"rubric": _RUBRIC}}


def _write_bundle(
    out_dir: Path,
    *,
    trial_id: str = "trial-1",
    grading_config: dict[str, Any] | None = None,
    include_task_description: bool = True,
) -> None:
    # filesystem_root must live OUTSIDE out_dir's tree: load_corpus_from_run treats
    # every subdirectory of a family directory as a bundle, so a sibling scratch
    # dir there would be mistaken for one. The system tempdir is always outside.
    fs_root = Path(tempfile.mkdtemp(prefix="judge-kind-ab-fs-"))
    serialize_grade_bundle(
        out_dir,
        trial_id=trial_id,
        initial_state={},
        final_state={},
        final_state_stable={},
        filesystem_root=fs_root,
        checks=None,
        kb=None,
        trajectory=_TRAJECTORY,
        grading_config=grading_config if grading_config is not None else _grading_config(),
        task_description=_TASK_DESCRIPTION if include_task_description else None,
    )


def test_corpus_entry_from_bundle_round_trips_rubric_transcript_and_state_diff(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "bundle"
    serialize_grade_bundle(
        out_dir,
        trial_id="trial-42",
        initial_state={"reservations": [{"id": 1, "seat": "12A"}]},
        final_state={"reservations": [{"id": 1, "seat": "14C"}]},
        final_state_stable={"reservations": [{"id": 1, "seat": "14C"}]},
        filesystem_root=tmp_path / "workspace",
        checks=None,
        kb=None,
        trajectory=_TRAJECTORY,
        grading_config=_grading_config(),
        task_description=_TASK_DESCRIPTION,
    )

    entry = corpus_entry_from_bundle(out_dir)

    assert entry.entry_id == "trial-42"
    assert entry.rubric.criteria[0].id == "helped"
    assert entry.agent_system_prompt == "You are a helpful airline agent."
    assert entry.transcript == [
        {"role": "user", "content": "Can you help me rebook my flight?"},
        {"role": "assistant", "content": "Sure, let me look into that."},
    ]
    assert entry.state_diff is not None
    assert "seat" in entry.state_diff
    assert entry.judge_scripts == {}


def test_corpus_entry_from_bundle_reports_no_state_diff_with_empty_initial_state(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "bundle"
    _write_bundle(out_dir)

    entry = corpus_entry_from_bundle(out_dir)

    assert entry.state_diff is None


def test_corpus_entry_from_bundle_raises_on_missing_llm_judge_rubric(tmp_path: Path) -> None:
    out_dir = tmp_path / "bundle"
    _write_bundle(out_dir, grading_config={})

    with pytest.raises(ValueError, match="llm_judge.rubric"):
        corpus_entry_from_bundle(out_dir)


def test_corpus_entry_from_bundle_raises_on_missing_task_description(tmp_path: Path) -> None:
    out_dir = tmp_path / "bundle"
    _write_bundle(out_dir, include_task_description=False)

    with pytest.raises(ValueError, match="task_description.json"):
        corpus_entry_from_bundle(out_dir)


def test_load_corpus_from_run_tags_entries_with_family_directory_name(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    _write_bundle(bundles_dir / "custom_checks" / "trial-1", trial_id="trial-1")
    _write_bundle(bundles_dir / "custom_checks" / "trial-2", trial_id="trial-2")
    _write_bundle(bundles_dir / "browser_task" / "trial-3", trial_id="trial-3")

    corpus = load_corpus_from_run(bundles_dir)

    families = sorted(family for family, _entry in corpus)
    assert families == ["browser_task", "custom_checks", "custom_checks"]
    entry_ids = sorted(entry.entry_id for _family, entry in corpus)
    assert entry_ids == ["trial-1", "trial-2", "trial-3"]
