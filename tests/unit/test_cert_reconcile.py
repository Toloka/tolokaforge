"""Unit tests for ``scripts/integration/cert_reconcile.py``.

``scripts/integration`` is not an importable package, so the module is loaded
from its file path (same approach as ``test_slack_notify.py``).

The scenario mirrors the real mimo PR #207 observe baseline, whose free-form
cert had exactly the two bugs this guard exists to catch:

* ``re2_pattern_tolerance`` passed 15/15 but was left UNDECLARED, and
* ``implicit_prompt_caching`` passed 14/15 (0.93) but was marked ``known_unsupported``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "integration" / "cert_reconcile.py"
_spec = importlib.util.spec_from_file_location("cert_reconcile", _MODULE_PATH)
cert_reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cert_reconcile)


# --- probe name -> capability mapping -----------------------------------------


def test_probe_base_strips_prefix_and_params():
    assert cert_reconcile.probe_base("test_dict_map_tool_call[simple-x]") == "dict_map_tool_call"
    assert cert_reconcile.probe_base("test_re2_pattern_tolerance[x]") == "re2_pattern_tolerance"


def test_probe_to_capability_exact_and_suffix():
    caps = [
        "dict_map_tool_call",
        "discriminated_union_tool_call",
        "prompt_caching",
        "implicit_prompt_caching",
    ]
    # exact match
    assert cert_reconcile.probe_to_capability("dict_map_tool_call", caps) == "dict_map_tool_call"
    # the discriminated-union test has a `_two_turns` suffix -> prefix match
    assert (
        cert_reconcile.probe_to_capability("discriminated_union_tool_call_two_turns", caps)
        == "discriminated_union_tool_call"
    )
    # longest-match: implicit must not be shadowed by prompt_caching, nor vice versa
    assert (
        cert_reconcile.probe_to_capability("implicit_prompt_caching", caps)
        == "implicit_prompt_caching"
    )
    assert cert_reconcile.probe_to_capability("prompt_caching", caps) == "prompt_caching"
    assert cert_reconcile.probe_to_capability("no_such_capability", caps) is None


def test_aggregate_probes_takes_min_across_params():
    caps = ["heterogeneous_array_tool_call", "dict_map_tool_call"]
    per_probe = [
        {"probe": "test_heterogeneous_array_tool_call[flat-x]", "passed": 15, "runs": 15},
        {"probe": "test_heterogeneous_array_tool_call[nested-x]", "passed": 0, "runs": 15},
        {"probe": "test_dict_map_tool_call[x]", "passed": 15, "runs": 15},
    ]
    agg = cert_reconcile.aggregate_probes(per_probe, caps)
    assert agg["heterogeneous_array_tool_call"] == 0.0  # min of 1.0 and 0.0
    assert agg["dict_map_tool_call"] == 1.0


# --- the reconcile rules ------------------------------------------------------


def _mimo_probed():
    """The real mimo #207 observe baseline (min pass_rate per capability)."""
    return {
        "basic_completion": 1.0,
        "dict_map_tool_call": 1.0,
        "decimal_field_tool_call": 1.0,
        "re2_pattern_tolerance": 1.0,
        "discriminated_union_tool_call": 0.0,  # fix-target (recovered by json_coerce)
        "recursive_ref_tool_call": 0.0,  # fix-target
        "implicit_prompt_caching": 0.933,  # 14/15
        "multi_turn_error_recovery": 0.867,  # 13/15
        "prompt_caching": 0.0,
        "thinking_emits_blocks": 0.0,
    }


def test_flags_undeclared_and_false_pessimism_on_the_agent_cert():
    # The agent's mimo cert: RE2 omitted; implicit_caching wrongly known_unsupported.
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
    violations, warnings = cert_reconcile.reconcile(required, known_unsupported, _mimo_probed())
    joined = " ".join(violations)
    # bug 1: re2 probed 15/15 but undeclared
    assert any("UNDECLARED" in v and "re2_pattern_tolerance" in v for v in violations), joined
    # bug 2: implicit_prompt_caching passes 0.93 but marked unsupported
    assert any(
        "FALSE-PESSIMISM" in v and "implicit_prompt_caching" in v for v in violations
    ), joined
    # borderline (not a hard failure): error-recovery at 0.867
    assert any("multi_turn_error_recovery" in w for w in warnings)
    assert not any("multi_turn_error_recovery" in v for v in violations)
    # genuine 0/15 ceilings must NOT be flagged. Use backtick-delimited tokens so the
    # `prompt_caching` check is not spuriously satisfied by the `implicit_prompt_caching` violation.
    assert not any("`prompt_caching`" in v and "FALSE" in v for v in violations)
    assert not any("`thinking_emits_blocks`" in v for v in violations)


def test_corrected_cert_reconciles_clean():
    # RE2 -> required; implicit_caching -> required (following the 0.93 baseline).
    required = {
        "basic_completion",
        "dict_map_tool_call",
        "decimal_field_tool_call",
        "re2_pattern_tolerance",
        "discriminated_union_tool_call",
        "recursive_ref_tool_call",
        "implicit_prompt_caching",
    }
    known_unsupported = {
        "multi_turn_error_recovery",  # 0.867 borderline -> warn only, allowed
        "prompt_caching",
        "thinking_emits_blocks",
    }
    violations, warnings = cert_reconcile.reconcile(required, known_unsupported, _mimo_probed())
    assert violations == []
    assert any("multi_turn_error_recovery" in w for w in warnings)


def test_core_capability_can_never_be_known_unsupported():
    # A pricing gap makes cost_usd fail live; laundering it into a known_unsupported
    # ceiling (baseline 0.0, so the false-pessimism >=0.9 rule does NOT catch it) must
    # still be blocked because cost_usd_populated is a CORE capability.
    violations, _ = cert_reconcile.reconcile(
        required={"basic_completion"},
        known_unsupported={"cost_usd_populated"},
        probed={"basic_completion": 1.0, "cost_usd_populated": 0.0},
        core=cert_reconcile.CORE_CAPABILITIES,
    )
    assert any(
        "CORE-UNSUPPORTED" in v and "cost_usd_populated" in v for v in violations
    ), violations
    # without the core set (default) it is NOT flagged - proves the guard is what catches it
    v2, _ = cert_reconcile.reconcile(
        required={"basic_completion"},
        known_unsupported={"cost_usd_populated"},
        probed={"basic_completion": 1.0, "cost_usd_populated": 0.0},
    )
    assert not any("CORE-UNSUPPORTED" in v for v in v2)


def test_core_capabilities_mirror_canon():
    # cert_reconcile hardcodes the core set (it is a path-loaded stdlib script that cannot
    # cheaply import the canon test module at runtime). Guard against drift: it MUST equal
    # tests/canonical/test_capability_registry.py::_CORE_CAPABILITIES (7 caps incl.
    # progress_after_success) - a missing entry would reopen the laundering loophole.
    from tests.canonical.test_capability_registry import _CORE_CAPABILITIES

    canon = {c.value for c in _CORE_CAPABILITIES}
    assert canon == cert_reconcile.CORE_CAPABILITIES, cert_reconcile.CORE_CAPABILITIES ^ canon
