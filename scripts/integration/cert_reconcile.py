#!/usr/bin/env python
"""Reconcile a candidate ``ModelCertificate`` against the observe ``findings.json``.

The resolve agent's certificate is free-form reasoning; it can diverge from the
measured observe baseline in two ways this gate catches (both seen on mimo, PR #207):

  1. COMPLETENESS - a capability that WAS probed in observe is left undeclared
     (neither ``required`` nor ``known_unsupported``). Undeclared capabilities
     auto-skip, silently dropping coverage. ADD_NEW_MODEL.md step 4 names this a
     contributor bug. (mimo: ``re2_pattern_tolerance`` passed 15/15 but was omitted.)

  2. FALSE-PESSIMISM - a capability the baseline shows PASSING (>= ``HARD_PASS``) is
     marked ``known_unsupported``, against the synthetic result. ADD_NEW_MODEL.md:
     "the cert MUST follow the synthetic test result, not your production hunch."
     (mimo: ``implicit_prompt_caching`` passed 14/15 = 0.93 but was marked unsupported.)

A capability in ``[SOFT_PASS, HARD_PASS)`` marked ``known_unsupported`` is WARNED,
not failed - a borderline demotion a human should confirm or re-sample.

Baseline pass_rate is the MIN across a capability's parametrised probes: a hard
false-pessimism flag therefore requires EVERY parameter to pass, so a genuinely
mixed capability (some params fail natively, recovered by the fix) is never
hard-failed here.

Exit 1 on any completeness violation or hard false-pessimism (the finalize
staged-tree gate routes that to needs-human); 0 otherwise. Warnings print but do
not fail. A load/import error exits 1 - a guard must fail loud, never skip.

Kept a tiny path-loaded script (like ``resolve_greencheck.py`` / ``slack_notify.py``)
so it runs before ``uv sync`` friction and stays unit-testable without the registry.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HARD_PASS = 0.9
SOFT_PASS = 0.8

# Mirror of tests/canonical/test_capability_registry.py::_CORE_CAPABILITIES - the
# capabilities every model MUST support. A core cap can NEVER be `known_unsupported`:
# e.g. `cost_usd_populated` marked a "ceiling" would launder a missing-pricing gap
# into a fake capability limit (ADD_NEW_MODEL.md step 1 + the cap's own docstring).
CORE_CAPABILITIES = frozenset(
    {
        "basic_completion",
        "simple_tool_call",
        "multi_turn_tool_use",
        "usage_metrics_populated",
        "cost_usd_populated",
        "required_fields_complete",
    }
)


def probe_base(node_id: str) -> str:
    """``test_dict_map_tool_call[simple-openrouter__x]`` -> ``dict_map_tool_call``."""
    name = node_id.split("[", 1)[0].strip()
    return name[len("test_") :] if name.startswith("test_") else name


def probe_to_capability(base: str, cap_values: list[str]) -> str | None:
    """Map a probe base name to a ``Capability`` value.

    A value maps 1:1 to ``test_<value>.py``, but a few tests carry a suffix (e.g.
    ``discriminated_union_tool_call`` -> ``test_discriminated_union_tool_call_two_turns``),
    so match on exact-or-``value_``-prefix and take the LONGEST matching value (so
    ``prompt_caching`` never shadows ``implicit_prompt_caching`` and vice versa).
    """
    matches = [v for v in cap_values if base == v or base.startswith(v + "_")]
    return max(matches, key=len) if matches else None


def aggregate_probes(per_probe: list[dict], cap_values: list[str]) -> dict[str, float]:
    """capability value -> MIN pass_rate across its parametrised probes."""
    agg: dict[str, float] = {}
    for entry in per_probe:
        node = entry.get("probe") or entry.get("name") or ""
        cap = probe_to_capability(probe_base(node), cap_values)
        if cap is None:
            continue
        runs = entry.get("runs") or 0
        rate = (entry.get("passed", 0) / runs) if runs else 0.0
        agg[cap] = min(agg.get(cap, 1.0), rate)
    return agg


def reconcile(
    required: set[str],
    known_unsupported: set[str],
    probed: dict[str, float],
    core: frozenset[str] = frozenset(),
    hard: float = HARD_PASS,
    soft: float = SOFT_PASS,
) -> tuple[list[str], list[str]]:
    """Return ``(violations, warnings)``. Pure - unit-tested without the registry."""
    declared = required | known_unsupported
    violations: list[str] = []
    warnings: list[str] = []
    for cap in sorted(known_unsupported & core):
        violations.append(
            f"CORE-UNSUPPORTED: `{cap}` is a CORE capability and must be `required` - a core cap "
            "can never be `known_unsupported` (e.g. cost_usd_populated as a ceiling hides a "
            "missing-pricing gap)."
        )
    for cap, rate in sorted(probed.items()):
        if cap not in declared:
            violations.append(
                f"UNDECLARED: `{cap}` was probed (baseline {rate:.2f}) but is neither "
                "`required` nor `known_unsupported` - it will silently auto-skip."
            )
        elif cap in known_unsupported and rate >= hard:
            violations.append(
                f"FALSE-PESSIMISM: `{cap}` is `known_unsupported` but the baseline PASSES "
                f"{rate:.2f} (>= {hard:.2f}) - the cert must follow the synthetic result; "
                "mark it `required`."
            )
        elif cap in known_unsupported and rate >= soft:
            warnings.append(
                f"BORDERLINE: `{cap}` is `known_unsupported` at baseline {rate:.2f} "
                f"([{soft:.2f}, {hard:.2f})) - confirm the demotion or re-sample."
            )
    return violations, warnings


def _load_cert(model_id: str) -> tuple[set[str], set[str], list[str]]:
    """Import the (staged) registry; return ``(required, known_unsupported, cap_values)``.

    Inserts the repo root on ``sys.path`` so ``tests`` imports regardless of cwd.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from tests.integration.llm._capability import Capability
    from tests.integration.llm.registry import ALL_MODELS

    cap_values = [c.value for c in Capability]
    for mc in ALL_MODELS:
        if mc.model_id == model_id:
            return (
                {c.value for c in mc.required},
                {c.value for c in mc.known_unsupported},
                cap_values,
            )
    raise SystemExit(f"cert_reconcile: model_id {model_id!r} not found in ALL_MODELS")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile a cert against observe findings.")
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--findings", required=True)
    args = ap.parse_args(argv)
    try:
        required, known_unsupported, cap_values = _load_cert(args.model_id)
        with open(args.findings) as handle:
            findings = json.load(handle)
    except SystemExit as exc:
        print(f"::error::{exc}")
        return 1
    except Exception as exc:  # fail loud - a guard must never silently pass
        print(f"::error::cert_reconcile could not load cert/findings: {exc}")
        return 1
    per_probe = (findings.get("capability") or {}).get("per_probe") or []
    probed = aggregate_probes(per_probe, cap_values)
    violations, warnings = reconcile(required, known_unsupported, probed, core=CORE_CAPABILITIES)
    for warning in warnings:
        print(f"::warning::cert_reconcile: {warning}")
    for violation in violations:
        print(f"::error::cert_reconcile: {violation}")
    if violations:
        print(f"cert_reconcile: FAIL ({len(violations)} violation(s), {len(warnings)} warning(s))")
        return 1
    print(f"cert_reconcile: OK ({len(probed)} probed caps reconciled, {len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
