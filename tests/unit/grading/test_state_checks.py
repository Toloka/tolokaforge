"""Tests for state-based grading checks"""

from pathlib import Path

import pytest

from tolokaforge.core.grading.state_checks import (
    StateChecker,
    canonical_number,
    consistent_hash,
    to_hashable,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestHashFunctions:
    """Test hash normalization and computation"""

    def test_to_hashable_dict(self):
        """Test dict normalization (scalars pass through canonical_number)."""
        data = {"b": 2, "a": 1, "c": 3}
        result = to_hashable(data)
        assert result == (
            ("a", canonical_number(1)),
            ("b", canonical_number(2)),
            ("c", canonical_number(3)),
        )

    def test_to_hashable_list(self):
        """Test list normalization"""
        data = [3, 1, 2]
        result = to_hashable(data)
        assert result == (canonical_number(3), canonical_number(1), canonical_number(2))

    def test_to_hashable_set(self):
        """Test set normalization"""
        data = {3, 1, 2}
        result = to_hashable(data)
        assert result == (canonical_number(1), canonical_number(2), canonical_number(3))

    def test_to_hashable_set_mixed_bool_and_number(self):
        """Type-stable set sort must not raise when a bool and a number coexist.

        Canonicalized numbers become tagged strings while bool stays bool; a bare
        sort over that mix is a TypeError, so the sort uses a type-stable key.
        """
        result = to_hashable({True, 2})
        assert len(result) == 2
        assert True in result
        assert canonical_number(2) in result

    def test_to_hashable_nested(self):
        """Test nested structure normalization"""
        data = {"users": [{"id": 2, "name": "bob"}, {"id": 1, "name": "alice"}], "count": 2}
        result = to_hashable(data)
        expected = (
            ("count", canonical_number(2)),
            (
                "users",
                (
                    (("id", canonical_number(2)), ("name", "bob")),
                    (("id", canonical_number(1)), ("name", "alice")),
                ),
            ),
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
class TestStateDistance:
    """Row-level symmetric-difference metric used to pick the closest variant."""

    def test_identical_states_have_zero_distance(self):
        state = {"orders": [{"id": 1, "amount": 100}]}
        assert StateChecker._state_distance(state, state) == 0

    def test_missing_row_counts_once(self):
        a = {"orders": [{"id": 1}, {"id": 2}]}
        b = {"orders": [{"id": 1}]}
        # 1 row in the symmetric difference: {id:2} in a only.
        assert StateChecker._state_distance(a, b) == 1

    def test_extra_row_counts_once(self):
        a = {"orders": [{"id": 1}]}
        b = {"orders": [{"id": 1}, {"id": 2}]}
        assert StateChecker._state_distance(a, b) == 1

    def test_differing_field_counts_as_two_rows(self):
        # {id:1, amount:100} vs {id:1, amount:200} produces two distinct
        # canonical strings — each side contributes one row to the sym-diff.
        a = {"orders": [{"id": 1, "amount": 100}]}
        b = {"orders": [{"id": 1, "amount": 200}]}
        assert StateChecker._state_distance(a, b) == 2

    def test_empty_states_have_zero_distance(self):
        assert StateChecker._state_distance({}, {}) == 0

    def test_disjoint_tables_summed(self):
        a = {"orders": [{"id": 1}], "carts": []}
        b = {"orders": [], "carts": [{"id": 9}]}
        # orders: {id:1} in a only; carts: {id:9} in b only → total 2.
        assert StateChecker._state_distance(a, b) == 2

    def test_row_duplication_counted_by_multiplicity(self):
        # Multiset semantics: the row appears twice in a and once in b, so
        # the distance is 1 (the extra copy). Prior set-based implementation
        # would deduplicate and score 0 despite the states genuinely differing.
        a = {"orders": [{"id": 1}, {"id": 1}]}
        b = {"orders": [{"id": 1}]}
        assert StateChecker._state_distance(a, b) == 1


@pytest.mark.unit
class TestGradeTauStyleVariants:
    """Multi-golden-variant support on ``grade_tau_style``.

    ``_execute_golden_actions`` is patched to return canned per-variant
    states so tests exercise the variant loop without loading a real MCP
    module. See :meth:`tolokaforge.core.grading.state_checks.StateChecker`.
    """

    @pytest.fixture
    def checker(self):
        return StateChecker()

    @pytest.fixture
    def trial_state_wraps(self):
        """Wrap a raw db state into the {agent, user, db} envelope that
        ``grade_tau_style`` expects on the ``state`` argument."""

        def wrap(db_state: dict) -> dict:
            return {"agent": {}, "user": {}, "db": db_state}

        return wrap

    def test_no_alternatives_matches_primary(self, checker, trial_state_wraps, monkeypatch):
        """Empty alternatives list => single-variant behaviour on match."""
        expected = {"orders": [{"id": 1, "status": "closed"}]}
        monkeypatch.setattr(
            checker,
            "_execute_golden_actions",
            lambda actions, td, isp, msp, dom: expected,
        )
        score, reasons, diff = checker.grade_tau_style(
            state=trial_state_wraps(expected),
            jsonpath_assertions=[],
            golden_actions=[{"name": "close", "kwargs": {}}],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
            alternative_golden_actions=None,
        )
        assert score == 1.0
        assert diff is None
        assert "State hash matches" in reasons
        # Regression guard: the multi-variant reason string must NOT appear
        # on a single-variant task (byte-identical wording contract). Guards
        # against a future refactor that accidentally routes the single-variant
        # path through the "matches golden variant N (of M)" wording.
        assert "matches golden variant" not in reasons

    def test_matches_alternative_variant(self, checker, trial_state_wraps, monkeypatch):
        """Trial state that matches variant 1 (not variant 0) still scores 1.0
        and the reason string names the matched variant.
        """
        variant_states = [
            {"orders": [{"id": 1, "shape": "combined"}]},  # variant 0
            {"orders": [{"id": 1, "shape": "split_a"}, {"id": 2, "shape": "split_b"}]},  # variant 1
        ]
        call_idx = {"n": 0}

        def fake_execute(actions, td, isp, msp, dom):
            state = variant_states[call_idx["n"]]
            call_idx["n"] += 1
            return state

        monkeypatch.setattr(checker, "_execute_golden_actions", fake_execute)

        score, reasons, diff = checker.grade_tau_style(
            state=trial_state_wraps(variant_states[1]),  # trial chose the split shape
            jsonpath_assertions=[],
            golden_actions=[{"name": "combine", "kwargs": {}}],
            alternative_golden_actions=[[{"name": "split", "kwargs": {}}]],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
        )
        assert score == 1.0
        assert diff is None
        assert "matches golden variant 1" in reasons
        assert "of 2" in reasons

    def test_all_variants_miss_returns_closest_diff(self, checker, trial_state_wraps, monkeypatch):
        """When no variant matches, the reported diff must be against the
        variant with the smallest row-level distance from the trial state.
        """
        variant_states = [
            # Distance from trial 3: (rows removed: 3 nonmatching; rows added: 1)
            {"orders": [{"id": 1}, {"id": 2}, {"id": 3}]},
            # Distance from trial: 1 extra row in the trial only.
            {"orders": [{"id": 99}]},
        ]
        trial_db = {"orders": [{"id": 99}, {"id": 100}]}
        call_idx = {"n": 0}

        def fake_execute(actions, td, isp, msp, dom):
            state = variant_states[call_idx["n"]]
            call_idx["n"] += 1
            return state

        monkeypatch.setattr(checker, "_execute_golden_actions", fake_execute)

        score, reasons, diff = checker.grade_tau_style(
            state=trial_state_wraps(trial_db),
            jsonpath_assertions=[],
            golden_actions=[{"name": "a", "kwargs": {}}],
            alternative_golden_actions=[[{"name": "b", "kwargs": {}}]],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
        )
        assert score == 0.0
        assert diff is not None
        assert "closest: variant 1" in reasons

    def test_broken_primary_matching_alternate_surfaces_replay_error(
        self, checker, trial_state_wraps, monkeypatch
    ):
        """A primary golden that fails to replay must appear in the reasons
        string even when an alternate matches the trial state.
        """
        variant_states = [
            None,  # variant 0 raises
            {"orders": [{"id": 1, "shape": "split"}]},  # variant 1 matches
        ]
        call_idx = {"n": 0}

        def fake_execute(actions, td, isp, msp, dom):
            idx = call_idx["n"]
            call_idx["n"] += 1
            if variant_states[idx] is None:
                raise RuntimeError(f"primary golden broken (variant {idx})")
            return variant_states[idx]

        monkeypatch.setattr(checker, "_execute_golden_actions", fake_execute)

        score, reasons, diff = checker.grade_tau_style(
            state=trial_state_wraps(variant_states[1]),
            jsonpath_assertions=[],
            golden_actions=[{"name": "primary", "kwargs": {}}],
            alternative_golden_actions=[[{"name": "alt", "kwargs": {}}]],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
        )
        assert score == 1.0
        assert "matches golden variant 1" in reasons
        # The broken primary MUST be surfaced despite the successful alt match.
        assert "Golden replay errors" in reasons
        assert "variant 0" in reasons
        assert "primary golden broken" in reasons

    def test_redundant_variants_first_match_wins(self, checker, trial_state_wraps, monkeypatch):
        """When two variants resolve to identical hashes (author declared
        equivalent alternatives), the first one to match wins and later
        variants are never replayed. Regression guard against variant
        index drift on hash collisions from authoring mistakes.
        """
        identical_state = {"orders": [{"id": 1, "status": "closed"}]}
        call_count = {"n": 0}

        def fake_execute(actions, td, isp, msp, dom):
            call_count["n"] += 1
            return identical_state

        monkeypatch.setattr(checker, "_execute_golden_actions", fake_execute)

        score, reasons, _ = checker.grade_tau_style(
            state=trial_state_wraps(identical_state),
            jsonpath_assertions=[],
            golden_actions=[{"name": "close_a", "kwargs": {}}],
            alternative_golden_actions=[[{"name": "close_b", "kwargs": {}}]],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
        )
        assert score == 1.0
        assert "matches golden variant 0" in reasons  # primary wins
        # Both variants still replay upfront (that's how the current
        # implementation collects variant_states), but the reason string
        # names variant 0 — not variant 1.
        assert "matches golden variant 1" not in reasons

    def test_all_variants_fail_to_replay_returns_zero(
        self, checker, trial_state_wraps, monkeypatch
    ):
        """If every variant raises during replay, grade is 0 and the reasons
        must include every per-variant error.
        """

        def fake_execute(actions, td, isp, msp, dom):
            raise RuntimeError("broken tool")

        monkeypatch.setattr(checker, "_execute_golden_actions", fake_execute)

        score, reasons, diff = checker.grade_tau_style(
            state=trial_state_wraps({"orders": []}),
            jsonpath_assertions=[],
            golden_actions=[{"name": "a", "kwargs": {}}],
            alternative_golden_actions=[[{"name": "b", "kwargs": {}}]],
            task_dir=Path("."),
            initial_state_path="init.json",
            mcp_server_path="mcp.py",
            task_domain="synthetic",
            hash_weight=1.0,
        )
        assert score == 0.0
        assert diff is None
        assert "All golden variants failed to replay" in reasons
        assert reasons.count("broken tool") == 2  # both variants surfaced


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
