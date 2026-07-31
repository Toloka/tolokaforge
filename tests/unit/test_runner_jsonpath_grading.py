"""Tests for evaluate_jsonpath_file_checks in runner/grading.py.

The Runner-side jsonpath check evaluates state_checks.jsonpath_checks
against files inside the runner container's /env/fs/agent-visible tree.
Adds a third grade component (jsonpath_score) that combines with hash
in state_checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.runner import grading as grading_module
from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
    evaluate_db_probes,
    evaluate_jsonpath_checks,
    evaluate_jsonpath_file_checks,
    evaluate_jsonpath_state_checks,
    resolve_state_checks_component,
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


@pytest.mark.parametrize(
    ("marker", "expected_score", "expected_reason"),
    [
        ("PASS", 1.0, "PASS: test suite passed"),
        ("FAIL", 0.0, "FAIL: test suite passed"),
    ],
)
def test_pass_marker_discriminates_against_fail(
    tmp_path: Path, marker: str, expected_score: float, expected_reason: str
):
    """A test-result marker file checked with ``contains_ci: "PASS"`` must score
    1.0 only when the marker says PASS. A file containing exactly ``FAIL`` must
    NOT vacuously satisfy the check — this is the decisive state-check floor the
    endpoint_add pack relies on, and using ``contains:`` instead of
    ``contains_ci:`` would silently always-pass because the missing key defaults
    to ``""`` and ``"" in content`` is vacuously True.
    """
    marker_file = tmp_path / "test_result.txt"
    marker_file.write_text(marker)

    checks = [
        {
            "path_glob": str(marker_file),
            "contains_ci": "PASS",
            "description": "test suite passed",
        }
    ]
    score, reasons = evaluate_jsonpath_file_checks(checks)
    assert score == expected_score
    assert expected_reason in reasons


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


def test_state_jsonpath_checks_grade_db_state():
    state = {
        "db": {
            "orders": [
                {
                    "status": "confirmed",
                    "items": [{"product_id": "hub-flex-7-silver", "quantity": 1}],
                }
            ]
        }
    }
    checks = [
        {
            "path": "$.db.orders[-1].status",
            "equals": "confirmed",
            "description": "order is confirmed",
        },
        {
            "path": "$.db.orders[-1].items[0].product_id",
            "equals": "hub-flex-7-silver",
            "description": "exact product selected",
        },
    ]

    score, reasons = evaluate_jsonpath_state_checks(checks, state)

    assert score == 1.0
    assert "PASS: order is confirmed" in reasons
    assert "PASS: exact product selected" in reasons


def test_state_jsonpath_checks_fail_on_wrong_value():
    state = {"db": {"orders": [{"status": "cancelled"}]}}
    checks = [
        {
            "path": "$.db.orders[-1].status",
            "equals": "confirmed",
            "description": "order is confirmed",
        }
    ]

    score, reasons = evaluate_jsonpath_state_checks(checks, state)

    assert score == 0.0
    assert "FAIL: order is confirmed" in reasons


def test_state_jsonpath_unknown_operator_fails_loud():
    """Parity with the core native grader (PR #66): a ``path:`` assertion with
    no recognized operator (here a typo'd ``op:``/``expected:``) must FAIL with
    an actionable reason, not silently pass as an existence check."""
    state = {"db": {"orders": [{"status": "cancelled"}]}}
    checks = [
        {
            "path": "$.db.orders[-1].status",
            "op": "gte",
            "expected": 5,
            "description": "bogus operator",
        }
    ]

    score, reasons = evaluate_jsonpath_state_checks(checks, state)

    assert score == 0.0
    assert "no recognized operator" in reasons
    assert "bogus operator" in reasons
    # The supported operators are named so the author can fix the assertion.
    assert "equals" in reasons


def test_mixed_jsonpath_checks_keep_file_compatibility(tmp_path: Path):
    target = tmp_path / "answer.md"
    target.write_text("approved")
    state = {"db": {"orders": [{"status": "confirmed"}]}}
    checks = [
        {
            "path_glob": str(target),
            "contains_ci": "approved",
            "description": "file artifact check",
        },
        {
            "path": "$.db.orders[-1].status",
            "equals": "confirmed",
            "description": "state check",
        },
    ]

    score, reasons = evaluate_jsonpath_checks(checks, state=state)

    assert score == 1.0
    assert "Files: PASS: file artifact check" in reasons
    assert "State: PASS: state check" in reasons


class TestStateChecksSlot:
    """Which of the three state sources fills the ``state_checks`` slot, and how.

    The runner's ``-1.0`` sentinel means *not evaluated*, so every case here pins
    the composed value — including ``None`` for a component no source produced,
    which is a different outcome from a ``0.0`` the combine would fold in as a
    failure.
    """

    @pytest.mark.parametrize(
        ("case", "hash_score", "jsonpath_score", "db_probe_score", "hash_weight", "expected"),
        [
            ("hash only", 1.0, -1.0, -1.0, None, 1.0),
            ("hash only, an inert weight is not consulted", 0.0, -1.0, -1.0, 0.6, 0.0),
            ("jsonpaths only", -1.0, 0.75, -1.0, None, 0.75),
            ("both, hash passing", 1.0, 0.5, -1.0, 0.6, 0.8),
            ("both, hash failing", 0.0, 0.5, -1.0, 0.6, 0.2),
            ("both, at a second weight", 1.0, 0.5, -1.0, 0.25, 0.625),
            ("both, the weight hands the verdict to the hash", 0.0, 0.5, -1.0, 1.0, 0.0),
            ("both, the weight hands the verdict to the jsonpaths", 0.0, 0.5, -1.0, 0.0, 0.5),
            ("neither", -1.0, -1.0, -1.0, None, None),
            ("db_probes alone", -1.0, -1.0, 0.4, None, 0.4),
            ("db_probes outrank a hash verdict", 1.0, -1.0, 0.4, None, 0.4),
            ("db_probes outrank a jsonpath score", -1.0, 0.75, 0.4, None, 0.4),
            ("db_probes outrank a fold of both", 1.0, 0.5, 0.4, 0.6, 0.4),
            ("db_probes outrank both before a weight is needed", 1.0, 0.5, 0.4, None, 0.4),
        ],
    )
    def test_composed_value(
        self, case, hash_score, jsonpath_score, db_probe_score, hash_weight, expected
    ):
        component = resolve_state_checks_component(
            hash_score=hash_score,
            jsonpath_score=jsonpath_score,
            db_probe_score=db_probe_score,
            hash_weight=hash_weight,
        )
        if expected is None:
            assert component is None
            return
        assert component == pytest.approx(expected)

    def test_two_real_sources_without_a_weight_raise_the_shared_message(self):
        with pytest.raises(ValueError, match="no defensible default"):
            resolve_state_checks_component(
                hash_score=1.0, jsonpath_score=0.5, db_probe_score=-1.0, hash_weight=None
            )


@pytest.mark.parametrize(("hash_weight", "expected"), [(0.6, 0.8), (0.25, 0.625)])
def test_combine_folds_by_the_weight_the_grading_config_carries(hash_weight, expected):
    components = {"hash_score": 1.0, "jsonpath_score": 0.5, "transcript_score": -1.0}
    grading_config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {
            "jsonpath_checks": [{"path_glob": "x"}],
            "hash_weight": hash_weight,
        },
    }
    score, _ = combine_grade_components(components, grading_config)
    assert score == pytest.approx(expected)


def test_combine_components_uses_jsonpath_when_hash_absent():
    components = {"hash_score": -1.0, "jsonpath_score": 0.75, "transcript_score": -1.0}
    grading_config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {"jsonpath_checks": [{"path_glob": "x"}]},
    }
    score, _ = combine_grade_components(components, grading_config)
    assert score == 0.75


def _stub_rows(rows, monkeypatch):
    """Inject an in-memory row set for _fetch_probe_rows (no live DB)."""

    async def fake_fetch(dsn: str, query: str):
        return rows

    monkeypatch.setattr(grading_module, "_fetch_probe_rows", fake_fetch)


async def test_db_probes_empty_returns_sentinel():
    score, reasons = await evaluate_db_probes([])
    assert score == -1.0
    assert reasons == ""


async def test_db_probe_passes_when_expect_matches(monkeypatch):
    _stub_rows([{"reason_code": "CAPA-01", "status": "open"}], monkeypatch)
    probes = [
        {
            "name": "ca_exists",
            "dsn": "postgresql://grader@app-db/mfg",
            "query": "SELECT reason_code, status FROM corrective_actions WHERE lot_id = 7",
            "expect": [
                {
                    "path": "$.rows[0].reason_code",
                    "equals": "CAPA-01",
                    "description": "reason code",
                },
                {"path": "$.rows[0].status", "equals": "open", "description": "status open"},
                {"path": "$.row_count", "equals": 1, "description": "exactly one row"},
            ],
            "description": "corrective action recorded",
        }
    ]
    score, reasons = await evaluate_db_probes(probes)
    assert score == 1.0
    assert "PASS: probe 'ca_exists'" in reasons


async def test_db_probe_fails_and_reports_actual_value(monkeypatch):
    _stub_rows([{"reason_code": "WRONG", "status": "open"}], monkeypatch)
    probes = [
        {
            "name": "ca_exists",
            "dsn": "postgresql://grader@app-db/mfg",
            "query": "SELECT reason_code FROM corrective_actions",
            "expect": [
                {
                    "path": "$.rows[0].reason_code",
                    "equals": "CAPA-01",
                    "description": "reason code",
                },
            ],
            "description": "corrective action recorded",
        }
    ]
    score, reasons = await evaluate_db_probes(probes)
    assert score == 0.0
    assert "FAIL: probe 'ca_exists'" in reasons
    # The mismatching value is surfaced for debugging.
    assert "WRONG" in reasons


async def test_db_probe_connection_error_fails_loud(monkeypatch):
    async def raising_fetch(dsn: str, query: str):
        raise ConnectionError("could not connect to app-db:5432")

    monkeypatch.setattr(grading_module, "_fetch_probe_rows", raising_fetch)
    probes = [
        {
            "name": "ca_exists",
            "dsn": "postgresql://grader@app-db/mfg",
            "query": "SELECT 1",
            "expect": [{"path": "$.row_count", "equals": 1}],
            "description": "corrective action recorded",
        }
    ]
    score, reasons = await evaluate_db_probes(probes)
    assert score == 0.0
    assert "FAIL: probe 'ca_exists'" in reasons
    assert "could not query postgres" in reasons
    assert "ConnectionError" in reasons


def test_combine_components_uses_db_probe_as_state_checks():
    components = {"hash_score": -1.0, "jsonpath_score": -1.0, "db_probe_score": 1.0}
    grading_config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {"db_probes": [{"name": "x"}]},
    }
    score, binary_pass = combine_grade_components(components, grading_config)
    assert score == 1.0
    assert binary_pass is True


def test_build_reasons_includes_db_probe_when_score_set():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "db_probe_score": 0.0,
        "db_probe_reasons": "FAIL: probe 'ca_exists' — FAIL: reason code",
        "transcript_score": -1.0,
    }
    text = build_grade_reasons(components)
    assert "DB probes: FAIL: probe 'ca_exists'" in text


def test_build_reasons_includes_jsonpath_when_score_set():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": 0.75,
        "jsonpath_reasons": "PASS: A; FAIL: B",
        "transcript_score": -1.0,
    }
    text = build_grade_reasons(components)
    assert "JSONPath: PASS: A; FAIL: B" in text


class TestRunnerSideAssertionsWithoutPathGlobFailLoud:
    """Pins the contract: runner-side jsonpath evaluator fails loudly with an
    actionable reason when an assertion doesn't carry ``path_glob``. Previously
    such assertions were silently skipped (presented as ``SKIP: No path_glob``
    while not counting as passed) — misrouted ``path:``-style assertions vanished
    from grading visibility, the symptom that surfaced on internal tasks with
    rich \\$.db.X jsonpaths.
    """

    def test_path_only_assertion_fails_loud_and_names_the_routing(self):
        """An assertion using ``path:`` (env-state JSONPath, host-side) gets a
        FAIL reason naming the wrong-evaluator routing — not a silent SKIP."""
        checks = [
            {
                "path": "$.db.orders[0].id",
                "equals": "O-001",
                "description": "First order is assigned ID O-001",
            }
        ]
        score, reasons = evaluate_jsonpath_file_checks(checks)
        # 0/1 — was effectively 0/1 before too, but is now explicit.
        assert score == 0.0
        # Old wording must be gone; new wording must explain what's wrong.
        assert "SKIP: No path_glob" not in reasons
        assert "FAIL" in reasons
        assert "graded host-side" in reasons
        assert "First order is assigned ID O-001" in reasons

    def test_truly_missing_target_still_fails_loud(self):
        """Assertion with neither ``path:`` nor ``path_glob:`` — generic
        actionable error pointing the author at the supported keys."""
        checks = [{"contains_ci": "x", "description": "no target"}]
        score, reasons = evaluate_jsonpath_file_checks(checks)
        assert score == 0.0
        assert "FAIL" in reasons
        assert "path_glob" in reasons
        assert "no target" in reasons
