"""Unit tests for ``automation.cert``.

The scenario mirrors the real mimo PR #207 observe baseline, whose free-form cert had
exactly the bugs this guard exists to catch: ``re2_pattern_tolerance`` passed 15/15 but
was UNDECLARED, and ``implicit_prompt_caching`` passed 14/15 (0.93) but was marked
``known_unsupported``.
"""

from __future__ import annotations

import automation.cert as cert
import pytest

pytestmark = pytest.mark.unit


def test_probe_base_strips_prefix_and_params():
    assert cert.probe_base("test_dict_map_tool_call[simple-x]") == "dict_map_tool_call"
    assert cert.probe_base("test_re2_pattern_tolerance[x]") == "re2_pattern_tolerance"


def test_probe_to_capability_exact_and_suffix():
    caps = [
        "dict_map_tool_call",
        "discriminated_union_tool_call",
        "prompt_caching",
        "implicit_prompt_caching",
    ]
    assert cert.probe_to_capability("dict_map_tool_call", caps) == "dict_map_tool_call"
    # the discriminated-union test has a `_two_turns` suffix -> prefix match
    assert (
        cert.probe_to_capability("discriminated_union_tool_call_two_turns", caps)
        == "discriminated_union_tool_call"
    )
    # longest-match: implicit must not be shadowed by prompt_caching, nor vice versa
    assert cert.probe_to_capability("implicit_prompt_caching", caps) == "implicit_prompt_caching"
    assert cert.probe_to_capability("prompt_caching", caps) == "prompt_caching"
    assert cert.probe_to_capability("no_such_capability", caps) is None


def test_aggregate_probes_takes_min_across_params():
    caps = ["heterogeneous_array_tool_call", "dict_map_tool_call"]
    per_probe = [
        {"probe": "test_heterogeneous_array_tool_call[flat-x]", "passed": 15, "runs": 15},
        {"probe": "test_heterogeneous_array_tool_call[nested-x]", "passed": 0, "runs": 15},
        {"probe": "test_dict_map_tool_call[x]", "passed": 15, "runs": 15},
    ]
    agg = cert.aggregate_probes(per_probe, caps)
    assert agg["heterogeneous_array_tool_call"] == 0.0
    assert agg["dict_map_tool_call"] == 1.0


def _mimo_probed():
    return {
        "basic_completion": 1.0,
        "dict_map_tool_call": 1.0,
        "decimal_field_tool_call": 1.0,
        "re2_pattern_tolerance": 1.0,
        "discriminated_union_tool_call": 0.0,
        "recursive_ref_tool_call": 0.0,
        "implicit_prompt_caching": 0.933,
        "multi_turn_error_recovery": 0.867,
        "prompt_caching": 0.0,
        "thinking_emits_blocks": 0.0,
    }


def test_flags_undeclared_and_false_pessimism_on_the_agent_cert():
    required = {
        "basic_completion",
        "dict_map_tool_call",
        "decimal_field_tool_call",
        "discriminated_union_tool_call",
        "recursive_ref_tool_call",
    }
    known_unsupported = {
        "implicit_prompt_caching",
        "multi_turn_error_recovery",
        "prompt_caching",
        "thinking_emits_blocks",
    }
    violations, warnings = cert.reconcile(required, known_unsupported, _mimo_probed())
    assert any("UNDECLARED" in v and "re2_pattern_tolerance" in v for v in violations)
    assert any("FALSE-PESSIMISM" in v and "implicit_prompt_caching" in v for v in violations)
    assert any("multi_turn_error_recovery" in w for w in warnings)
    assert not any("multi_turn_error_recovery" in v for v in violations)
    assert not any("`prompt_caching`" in v and "FALSE" in v for v in violations)
    assert not any("`thinking_emits_blocks`" in v for v in violations)


def test_corrected_cert_reconciles_clean():
    required = {
        "basic_completion",
        "dict_map_tool_call",
        "decimal_field_tool_call",
        "re2_pattern_tolerance",
        "discriminated_union_tool_call",
        "recursive_ref_tool_call",
        "implicit_prompt_caching",
    }
    known_unsupported = {"multi_turn_error_recovery", "prompt_caching", "thinking_emits_blocks"}
    violations, warnings = cert.reconcile(required, known_unsupported, _mimo_probed())
    assert violations == []
    assert any("multi_turn_error_recovery" in w for w in warnings)


def test_core_capability_can_never_be_known_unsupported():
    violations, _ = cert.reconcile(
        required={"basic_completion"},
        known_unsupported={"cost_usd_populated"},
        probed={"basic_completion": 1.0, "cost_usd_populated": 0.0},
        core=cert.CORE_CAPABILITIES,
    )
    assert any("CORE-UNSUPPORTED" in v and "cost_usd_populated" in v for v in violations)
    v2, _ = cert.reconcile(
        required={"basic_completion"},
        known_unsupported={"cost_usd_populated"},
        probed={"basic_completion": 1.0, "cost_usd_populated": 0.0},
    )
    assert not any("CORE-UNSUPPORTED" in v for v in v2)


def test_core_capabilities_mirror_canon():
    # Guard against drift: the hardcoded set MUST equal the canonical one (7 caps incl.
    # progress_after_success) - a missing entry would reopen the laundering loophole.
    from tests.canonical.test_capability_registry import _CORE_CAPABILITIES

    canon = {c.value for c in _CORE_CAPABILITIES}
    assert canon == cert.CORE_CAPABILITIES, cert.CORE_CAPABILITIES ^ canon
