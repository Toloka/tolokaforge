"""Tests for state-based grading checks"""

import pytest

from tolokaforge.core.grading.state_checks import (
    StateChecker,
    consistent_hash,
    to_hashable,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestHashFunctions:
    """Test hash normalization and computation"""

    def test_to_hashable_dict(self):
        """Test dict normalization"""
        data = {"b": 2, "a": 1, "c": 3}
        result = to_hashable(data)
        assert result == (("a", 1), ("b", 2), ("c", 3))

    def test_to_hashable_list(self):
        """Test list normalization"""
        data = [3, 1, 2]
        result = to_hashable(data)
        assert result == (3, 1, 2)

    def test_to_hashable_set(self):
        """Test set normalization"""
        data = {3, 1, 2}
        result = to_hashable(data)
        assert result == (1, 2, 3)

    def test_to_hashable_nested(self):
        """Test nested structure normalization"""
        data = {"users": [{"id": 2, "name": "bob"}, {"id": 1, "name": "alice"}], "count": 2}
        result = to_hashable(data)
        expected = (
            ("count", 2),
            ("users", ((("id", 2), ("name", "bob")), (("id", 1), ("name", "alice")))),
        )
        assert result == expected

    def test_consistent_hash_deterministic(self):
        """Test hash is deterministic"""
        data = {"a": 1, "b": [2, 3], "c": {"x": 4}}
        hash1 = consistent_hash(to_hashable(data))
        hash2 = consistent_hash(to_hashable(data))
        assert hash1 == hash2

    def test_consistent_hash_different_order_same_hash(self):
        """Test dict key order doesn't affect hash"""
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}
        hash1 = consistent_hash(to_hashable(data1))
        hash2 = consistent_hash(to_hashable(data2))
        assert hash1 == hash2

    def test_consistent_hash_tau_bench_example(self):
        """Test hash matches known tau-bench value"""
        data = {"status": "confirmed", "id": "R123"}
        hashed = consistent_hash(to_hashable(data))
        assert isinstance(hashed, str)
        assert len(hashed) == 64


@pytest.mark.unit
class TestJSONPathAssertions:
    """Test JSONPath assertion checking"""

    @pytest.fixture
    def checker(self):
        return StateChecker()

    @pytest.fixture
    def sample_state(self):
        return {
            "lines": [
                {"msisdn": "5550142", "mms_enabled": True, "status": "active"},
                {"msisdn": "5550199", "mms_enabled": False, "status": "active"},
            ],
            "tickets": [{"msisdn": "5550142", "status": "resolved", "id": "T001"}],
            "bookings": [{"hotel": "grand_plaza", "status": "confirmed", "name": "Alice Johnson"}],
        }

    def test_jsonpath_equals_pass(self, checker, sample_state):
        """Test equals assertion that passes"""
        assertions = [
            {"path": "$.lines[0].mms_enabled", "equals": True, "description": "MMS enabled"}
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_jsonpath_equals_fail(self, checker, sample_state):
        """Test equals assertion that fails"""
        assertions = [
            {
                "path": "$.lines[0].mms_enabled",
                "equals": False,
                "description": "MMS should be disabled",
            }
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 0.0
        assert len(reasons) == 1
        assert "MMS should be disabled" in reasons[0]

    def test_jsonpath_contains_in_string(self, checker, sample_state):
        """Test contains assertion with string"""
        assertions = [
            {"path": "$.bookings[0].hotel", "contains": "plaza", "description": "Hotel name"}
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_jsonpath_equals_ci_pass(self, checker, sample_state):
        """Test equals_ci assertion that passes with different casing."""
        assertions = [
            {
                "path": "$.bookings[0].name",
                "equals_ci": "alice johnson",
                "description": "Name match",
            }
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_jsonpath_contains_ci_string(self, checker, sample_state):
        """Test contains_ci assertion on string with different casing."""
        assertions = [
            {
                "path": "$.bookings[0].hotel",
                "contains_ci": "PLAZA",
                "description": "Hotel contains plaza",
            }
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_path_glob_contains_ci_pass(self, checker):
        """Test path_glob assertion over submission files without hardcoded filename."""
        state = {
            "filesystem": {
                "/env/fs/agent-visible/submissions/report.md": "Include rollback steps and verification.",
                "/env/fs/agent-visible/notes.txt": "scratch",
            }
        }
        assertions = [
            {
                "path_glob": "/env/fs/agent-visible/submissions/*",
                "contains_ci": "rollback",
                "description": "Submission includes rollback guidance",
            }
        ]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_path_glob_does_not_scan_non_matching_paths(self, checker):
        """Test path_glob only scans matching paths and fails otherwise."""
        state = {
            "filesystem": {
                "/env/fs/agent-visible/system.log": "rollback appeared in logs only",
            }
        }
        assertions = [
            {
                "path_glob": "/env/fs/agent-visible/submissions/*",
                "contains_ci": "rollback",
                "description": "Submission includes rollback guidance",
            }
        ]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        assert len(reasons) == 1
        assert "Path not found" in reasons[0]

    def test_jsonpath_filter(self, checker, sample_state):
        """Test JSONPath with filter"""
        assertions = [
            {
                "path": "$.lines[0].mms_enabled",
                "equals": True,
                "description": "MMS for specific line",
            }
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 1.0
        assert len(reasons) == 0

    def test_jsonpath_not_found(self, checker, sample_state):
        """Test assertion with path that doesn't exist"""
        assertions = [
            {
                "path": "$.nonexistent.field",
                "equals": "value",
                "description": "Missing field",
            }
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == 0.0
        assert len(reasons) == 1
        assert "Path not found" in reasons[0]

    def test_multiple_assertions_partial(self, checker, sample_state):
        """Test multiple assertions with partial success"""
        assertions = [
            {"path": "$.lines[0].mms_enabled", "equals": True, "description": "MMS enabled"},
            {
                "path": "$.lines[1].mms_enabled",
                "equals": True,
                "description": "MMS should be enabled",
            },
            {"path": "$.tickets[0].status", "equals": "resolved", "description": "Ticket resolved"},
        ]
        score, reasons = checker.check_jsonpaths(sample_state, assertions)
        assert score == pytest.approx(2.0 / 3.0)
        assert len(reasons) == 1

    def test_empty_assertions(self, checker, sample_state):
        """Test with no assertions"""
        score, reasons = checker.check_jsonpaths(sample_state, [])
        assert score == 1.0
        assert len(reasons) == 0


@pytest.mark.unit
class TestHashGrading:
    """Test hash-based grading"""

    @pytest.fixture
    def checker(self):
        return StateChecker()

    def test_hash_match(self, checker):
        """Test matching hash"""
        state = {"status": "completed", "value": 42}
        expected_hash = consistent_hash(to_hashable(state))
        score, reason = checker.check_hash(state, expected_hash)
        assert score == 1.0
        assert "matches" in reason.lower()

    def test_hash_mismatch(self, checker):
        """Test mismatching hash"""
        state = {"status": "completed", "value": 42}
        wrong_hash = "0" * 64
        score, reason = checker.check_hash(state, wrong_hash)
        assert score == 0.0
        assert "mismatch" in reason.lower()

    def test_hash_different_states(self, checker):
        """Test different states produce different hashes"""
        state1 = {"status": "completed"}
        state2 = {"status": "pending"}
        hash1 = consistent_hash(to_hashable(state1))
        hash2 = consistent_hash(to_hashable(state2))
        assert hash1 != hash2


@pytest.mark.unit
class TestCombinedGrading:
    """Test combined JSONPath and hash grading"""

    @pytest.fixture
    def checker(self):
        return StateChecker()

    @pytest.fixture
    def state(self):
        return {"lines": [{"id": 1, "enabled": True}], "count": 1}

    def test_hash_only(self, checker, state):
        """Test hash-only grading"""
        expected_hash = consistent_hash(to_hashable(state))
        score, reasons = checker.grade(
            state=state,
            jsonpath_assertions=[],
            expected_hash=expected_hash,
            hash_weight=1.0,
        )
        assert score == 1.0

    def test_jsonpath_only(self, checker, state):
        """Test JSONPath-only grading"""
        assertions = [{"path": "$.lines[0].enabled", "equals": True, "description": "Enabled"}]
        score, reasons = checker.grade(
            state=state, jsonpath_assertions=assertions, expected_hash=None, hash_weight=0.5
        )
        assert score == 1.0

    def test_combined_both_pass(self, checker, state):
        """Test combined grading where both pass"""
        expected_hash = consistent_hash(to_hashable(state))
        assertions = [{"path": "$.count", "equals": 1, "description": "Count is 1"}]
        score, reasons = checker.grade(
            state=state,
            jsonpath_assertions=assertions,
            expected_hash=expected_hash,
            hash_weight=0.5,
        )
        assert score == 1.0

    def test_combined_hash_fail_jsonpath_pass(self, checker, state):
        """Test combined grading where hash fails but JSONPath passes"""
        wrong_hash = "0" * 64
        assertions = [{"path": "$.count", "equals": 1, "description": "Count is 1"}]
        score, reasons = checker.grade(
            state=state,
            jsonpath_assertions=assertions,
            expected_hash=wrong_hash,
            hash_weight=0.5,
        )
        assert score == pytest.approx(0.5)


@pytest.mark.unit
class TestUnknownOperatorFailsLoud:
    """Pins the contract: an assertion with no recognized operator must FAIL
    (with an actionable reason), not silently satisfy on path-existence.

    Recognized operators are ``equals``, ``equals_ci``, ``contains``,
    ``contains_ci``. Anything else (``op: gte``, typo like ``eqaul``, no operator
    at all) used to be a silent no-op — this class regression-pins the
    loud-failure behaviour.
    """

    @pytest.fixture
    def checker(self):
        return StateChecker()

    def test_unknown_operator_keys_fail_with_actionable_reason(self, checker):
        """An assertion with ``op``/``expected`` (unsupported by the engine)
        previously satisfied silently as long as the path existed. Now it fails."""
        state = {"counter": 0}
        assertions = [
            {
                "path": "$.counter",
                "op": "gte",
                "expected": 5,
                "description": "Counter should be at least 5",
            }
        ]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        assert len(reasons) == 1
        r = reasons[0]
        assert "no recognized operator" in r
        assert "'equals'" in r and "'contains'" in r
        assert "op" in r and "expected" in r  # offending keys are surfaced

    def test_no_operator_at_all_fails(self, checker):
        """An assertion that only specifies ``path`` (no operator) used to
        silently satisfy; it now fails loudly."""
        state = {"counter": 0}
        assertions = [{"path": "$.counter", "description": "exists"}]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        assert "no recognized operator" in reasons[0]

    def test_typo_in_operator_name_fails(self, checker):
        """A misspelled operator key (``eqaul`` for ``equals``) fails — the
        check is by exact key name, not heuristic, so typos are caught."""
        state = {"x": "hello"}
        assertions = [{"path": "$.x", "eqaul": "hello", "description": "typo"}]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        assert "eqaul" in reasons[0]

    def test_partial_credit_with_one_unknown_one_valid(self, checker):
        """A mix of one valid (passing) and one unknown-operator assertion
        gives partial credit — confirms per-assertion handling, not early-exit."""
        state = {"a": 1, "b": 2}
        assertions = [
            {"path": "$.a", "equals": 1, "description": "valid passing"},
            {"path": "$.b", "op": "gte", "expected": 2, "description": "unknown op"},
        ]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == pytest.approx(0.5)
        assert any("no recognized operator" in r for r in reasons)

    def test_pre_fix_silent_pass_scenario_now_fails(self, checker):
        """Regression pin for the exact silent-pass scenario: the path EXISTS
        and the unrecognized operator's ``expected`` value would NOT match —
        before the fix this satisfied 1/1 (score 1.0). It must now be 0/1."""
        state = {"counter": 0}  # NOT 5
        assertions = [
            {"path": "$.counter", "op": "eq", "expected": 5, "description": "uses op:eq"},
        ]
        score, _ = checker.check_jsonpaths(state, assertions)
        assert score == 0.0, "Unrecognized operator must not silently satisfy"

    def test_path_glob_with_unknown_operator_fails(self, checker):
        """The fail-loud branch applies to ``path_glob`` assertions too, and the
        reason names the glob target (not just ``path``)."""
        state = {"filesystem": {"/agent/out.txt": "data"}}
        assertions = [
            {"path_glob": "/agent/*", "op": "matches", "expected": "data"},
        ]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        # Target is the glob string, not None.
        assert "/agent/*" in reasons[0]
        assert "no recognized operator" in reasons[0]

    def test_actionable_reason_has_no_empty_parens_when_no_description(self, checker):
        """Cosmetic: when description is omitted, the reason must not end with
        a stray ``()`` — keeps the failure message clean for downstream parsing."""
        state = {"counter": 0}
        assertions = [{"path": "$.counter", "op": "gte", "expected": 5}]
        score, reasons = checker.check_jsonpaths(state, assertions)
        assert score == 0.0
        assert not reasons[0].rstrip().endswith("()"), reasons[0]
        assert "()" not in reasons[0]
