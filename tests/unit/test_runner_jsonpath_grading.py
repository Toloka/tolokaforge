"""Tests for evaluate_jsonpath_file_checks in runner/grading.py.

The Runner-side jsonpath check evaluates state_checks.jsonpath_checks
against files inside the runner container's /env/fs/agent-visible tree.
Adds a third grade component (jsonpath_score) that combines with hash
in state_checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
    evaluate_jsonpath_file_checks,
)

pytestmark = pytest.mark.unit


def test_empty_checks_returns_sentinel():
    score, reasons = evaluate_jsonpath_file_checks([])
    assert score == -1.0
    assert reasons == ""


def test_check_passes_when_file_contains_expected_text(tmp_path: Path):
    target = tmp_path / "submissions" / "answer.md"
    target.parent.mkdir()
    target.write_text("This deal is NOT PERMITTED under section 4.")

    checks = [
        {
            "path_glob": str(tmp_path / "submissions" / "*"),
            "contains_ci": "not permitted",
            "description": "policy gate",
        }
    ]
    score, reasons = evaluate_jsonpath_file_checks(checks)
    assert score == 1.0
    assert "PASS: policy gate" in reasons


def test_check_fails_when_file_missing_expected_text(tmp_path: Path):
    target = tmp_path / "out.txt"
    target.write_text("nothing relevant here")

    checks = [
        {
            "path_glob": str(tmp_path / "*.txt"),
            "contains_ci": "expected substring",
            "description": "must mention X",
        }
    ]
    score, reasons = evaluate_jsonpath_file_checks(checks)
    assert score == 0.0
    assert "FAIL: must mention X" in reasons


def test_check_fails_when_no_files_match_glob(tmp_path: Path):
    checks = [
        {
            "path_glob": str(tmp_path / "missing" / "*"),
            "contains_ci": "anything",
            "description": "no files",
        }
    ]
    score, reasons = evaluate_jsonpath_file_checks(checks)
    assert score == 0.0
    assert "No files match" in reasons


def test_logical_agent_visible_path_translates_to_work(tmp_path: Path, monkeypatch):
    """`/env/fs/agent-visible/X` glob should match files written under /work/X.

    The runner provisions and the file tools operate under /work/; grading
    must look at the same directory or every file check fails with
    'No files match'.
    """
    # Arrange a file under tmp_path/work/submissions/answer.md and pretend
    # tmp_path is the container root so /work/ resolves to tmp_path/work/.
    work_dir = tmp_path / "work" / "submissions"
    work_dir.mkdir(parents=True)
    (work_dir / "answer.md").write_text("the answer is FORTY-TWO")

    import glob as glob_module

    captured: list[str] = []
    real_glob = glob_module.glob

    def patched_glob(pattern: str, *args, **kwargs):  # noqa: ANN001
        captured.append(pattern)
        # Rewrite the absolute /work/... pattern under tmp_path so the test
        # doesn't need to write into the real container's /work directory.
        if pattern.startswith("/work/"):
            pattern = str(tmp_path / "work" / pattern[len("/work/") :])
        elif pattern == "/work":
            pattern = str(tmp_path / "work")
        return real_glob(pattern, *args, **kwargs)

    monkeypatch.setattr("tolokaforge.runner.grading.glob.glob", patched_glob)

    checks = [
        {
            "path_glob": "/env/fs/agent-visible/submissions/*.md",
            "contains_ci": "forty-two",
            "description": "answer file contains the magic phrase",
        }
    ]
    score, reasons = evaluate_jsonpath_file_checks(checks)

    # The translated /work/ pattern was actually fed to glob — proof that
    # the logical path was rewritten before globbing.
    assert any(p.startswith("/work/") for p in captured)
    assert score == 1.0
    assert "PASS: answer file contains the magic phrase" in reasons


def test_partial_pass_score(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    checks = [
        {"path_glob": str(tmp_path / "a.txt"), "contains_ci": "alpha", "description": "A"},
        {"path_glob": str(tmp_path / "a.txt"), "contains_ci": "missing", "description": "B"},
    ]
    score, _ = evaluate_jsonpath_file_checks(checks)
    assert score == 0.5


def test_combine_components_uses_jsonpath_when_hash_absent():
    components = {"hash_score": -1.0, "jsonpath_score": 0.75, "transcript_score": -1.0}
    grading_config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {"jsonpath_checks": [{"path_glob": "x"}]},
    }
    score, _ = combine_grade_components(components, grading_config)
    assert score == 0.75


def test_combine_components_multiplies_hash_and_jsonpath():
    components = {"hash_score": 1.0, "jsonpath_score": 0.5, "transcript_score": -1.0}
    grading_config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {"jsonpath_checks": [{"path_glob": "x"}]},
    }
    score, _ = combine_grade_components(components, grading_config)
    assert score == pytest.approx(0.5)


def test_build_reasons_includes_jsonpath_when_score_set():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": 0.75,
        "jsonpath_reasons": "PASS: A; FAIL: B",
        "transcript_score": -1.0,
    }
    text = build_grade_reasons(components)
    assert "Files: PASS: A; FAIL: B" in text
