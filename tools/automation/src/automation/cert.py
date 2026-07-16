"""Reconcile a candidate ``ModelCertificate`` against the observe ``findings.json``.

The resolve agent's certificate is free-form reasoning; it can diverge from the
measured observe baseline in three ways this gate catches:

  1. COMPLETENESS - a capability that WAS probed is left undeclared (neither
     ``required`` nor ``known_unsupported``); it would silently auto-skip.
  2. FALSE-PESSIMISM - a capability the baseline shows PASSING (>= ``HARD_PASS``) is
     marked ``known_unsupported``, against the synthetic result.
  3. CORE-UNSUPPORTED - a CORE capability (e.g. ``cost_usd_populated``) is marked
     ``known_unsupported`` - which would launder a missing-pricing gap into a fake
     ceiling.

A capability in ``[SOFT_PASS, HARD_PASS)`` marked ``known_unsupported`` is WARNED,
not failed. Baseline pass_rate is the MIN across a capability's parametrised probes,
so a hard false-pessimism flag requires EVERY parameter to pass.

``run`` returns an exit code (1 on any violation, 0 otherwise); the pure helpers
below are unit-tested without importing the engine registry.
"""

from __future__ import annotations

import json
import pathlib
import sys

HARD_PASS = 0.9
SOFT_PASS = 0.8

# Mirror of tests/canonical/test_capability_registry.py::_CORE_CAPABILITIES - the
# capabilities every model MUST support. A core cap can NEVER be ``known_unsupported``
# (a drift guard test asserts this set equals the canonical one).
CORE_CAPABILITIES = frozenset(
    {
        "basic_completion",
        "simple_tool_call",
        "multi_turn_tool_use",
        "usage_metrics_populated",
        "cost_usd_populated",
        "required_fields_complete",
        "progress_after_success",
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

    Inserts the repo root on ``sys.path`` so ``tests`` imports regardless of cwd. The
    repo root is four levels up from this file (tools/automation/src/automation).
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))
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


def run(model_id: str, findings_path: str) -> int:
    """Reconcile the staged cert for ``model_id`` against ``findings_path``. Returns an
    exit code: 1 on any violation (finalize routes that to needs-human), 0 otherwise. A
    load/import error exits 1 - a guard must fail loud, never skip."""
    try:
        required, known_unsupported, cap_values = _load_cert(model_id)
        with open(findings_path) as handle:
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
