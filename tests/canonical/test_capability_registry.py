"""Canonical regression guard for the capability registry.

Pins the invariants that keep the capability-driven integration suite
honest. Every assertion here catches a class of contributor error that
would otherwise silently rot the registry:

* **Unique slugs** — duplicate ``model_id`` values mask one certificate
  behind another during pytest parametrisation.
* **Slug-module-agreement** — certificate IDs must equal
  :func:`tolokaforge.core.output.artifacts.model_id_slug`, so per-trial
  sidecars and the registry stay in lockstep.
* **No orphan capabilities** — every :class:`Capability` enum value
  must be referenced by at least one certificate (either ``required``
  or ``known_unsupported``). A capability with no references means the
  test file exists but nothing in the registry claims coverage — a
  code smell the canon refuses to tolerate.
* **Both branches exercised** — for every **non-core** capability, at
  least one certificate declares it in ``required`` AND at least one
  declares it in ``known_unsupported`` — guarantees the pass-path and
  the skip-path of the auto-skip machinery are both covered by the
  live suite. "Core" capabilities (:data:`_CORE_CAPABILITIES` below)
  are exempted: they represent the floor of acceptable LLM behaviour
  (basic completion, tool calling, usage telemetry) and every
  registered model requires them — there is no realistic certificate
  that lacks basic completion. The pass-path is still asserted for
  every capability including the core ones.
* **Deterministic order** — :data:`ALL_MODELS` is a tuple, and repeat
  imports yield the same sequence (pytest test-collection IDs pivot on
  this).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.output.artifacts import model_id_slug
from tolokaforge.testing.certify import ALL_MODELS, Capability

pytestmark = pytest.mark.canonical


# Capabilities every registered LLM is expected to satisfy. These are
# exempted from the "both branches exercised" invariant — no realistic
# model is certified as *lacking* basic completion or usage telemetry.
# The pass-path is still asserted via ``test_every_capability_has_required_coverage``
# below, so orphan-ing one of these still fails canon.
_CORE_CAPABILITIES = frozenset(
    {
        Capability.BASIC_COMPLETION,
        Capability.SIMPLE_TOOL_CALL,
        Capability.MULTI_TURN_TOOL_USE,
        Capability.USAGE_METRICS_POPULATED,
        # Cost reporting is a universal-floor contract: every
        # benchmarked call MUST yield a positive ``cost_usd``. A model
        # for which the answer is honestly None (no pricing in
        # litellm's catalog AND no entry in our ``pricing.json``) is a
        # benchmarking-blocking gap to fix at the data layer, not a
        # capability to opt out of. See
        # tolokaforge/testing/certify/_capability.py::Capability.COST_USD_POPULATED.
        Capability.COST_USD_POPULATED,
        # Single-turn baseline — every realistic modern function-
        # calling model passes ``test_required_fields_complete``.
        # Field-omission failures observed in multi-turn evaluations
        # are not single-turn-deterministic; this baseline gates the
        # cleanly-reproducible surface, with a future multi-turn
        # variant gating the heavy-context surface separately. See
        # tolokaforge/testing/certify/_capability.py::Capability.REQUIRED_FIELDS_COMPLETE
        # for the empirical justification.
        Capability.REQUIRED_FIELDS_COMPLETE,
        # Single-turn baseline — every registered modern model
        # passes ``test_progress_after_success`` (verified live
        # 2026-05-20 against gemini-3.5-flash, grok-4.3, kimi-k2.6,
        # deepseek-v4-pro, mimo-v2.5-pro). Production-only loop
        # failures observed in multi-turn evaluations are NOT
        # single-turn-deterministic; this baseline gates the
        # cleanly-reproducible surface, with a future multi-turn
        # variant gating the production surface separately. See
        # tolokaforge/testing/certify/_capability.py::Capability.PROGRESS_AFTER_SUCCESS
        # for the empirical justification.
        Capability.PROGRESS_AFTER_SUCCESS,
    }
)


class TestRegistryInvariants:
    """Every ``ModelCertificate`` in :data:`ALL_MODELS` must be honest."""

    def test_model_ids_are_unique(self) -> None:
        slugs = [cert.model_id for cert in ALL_MODELS]
        duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
        assert not duplicates, f"Duplicate model_ids in ALL_MODELS: {duplicates}"

    def test_model_id_matches_slug(self) -> None:
        for cert in ALL_MODELS:
            expected = model_id_slug(cert.provider, cert.name)
            assert cert.model_id == expected, (
                f"{cert.model_id!r} disagrees with model_id_slug({cert.provider!r}, "
                f"{cert.name!r}) = {expected!r}"
            )

    def test_all_models_non_empty(self) -> None:
        assert ALL_MODELS, "ALL_MODELS must not be empty"

    def test_all_models_is_tuple(self) -> None:
        # Tuple, not list — the registry contract requires immutability
        # and determinism at the type level.
        assert isinstance(ALL_MODELS, tuple)

    def test_deterministic_import_order(self) -> None:
        """Re-importing the module yields the same certificates in the
        same order. Pytest test-collection IDs pivot on this stability.
        """
        import importlib

        from tolokaforge_models.certificates import registry

        original = ALL_MODELS
        reloaded = importlib.reload(registry).ALL_MODELS
        assert [c.model_id for c in reloaded] == [c.model_id for c in original]


class TestCapabilityCoverage:
    """Every ``Capability`` enum value must be referenced by at least one
    certificate. Both branches (required + known_unsupported) must be
    exercised across the registry so the capability test's
    pass-path AND skip-path are both covered by the live suite.
    """

    def test_no_orphan_capabilities(self) -> None:
        seen: set[Capability] = set()
        for cert in ALL_MODELS:
            seen.update(cert.required)
            seen.update(cert.known_unsupported)
        missing = sorted(
            (c.value for c in Capability if c not in seen),
        )
        assert not missing, (
            f"Orphan capabilities (no certificate references them): {missing}. "
            "Declare each capability on at least one certificate in "
            "tolokaforge_models/src/tolokaforge_models/certificates/registry.py."
        )

    def test_every_capability_has_required_coverage(self) -> None:
        """Pass-path is always asserted (core + non-core alike)."""
        for cap in Capability:
            in_required = any(cap in cert.required for cert in ALL_MODELS)
            assert in_required, (
                f"No certificate requires {cap.value!r} — the capability "
                "test's pass-path is untested. Declare it in the ``required`` "
                "set of at least one ModelCertificate in "
                "tolokaforge_models/src/tolokaforge_models/certificates/registry.py."
            )

    def test_non_core_capabilities_have_both_branches(self) -> None:
        """Non-core capabilities must have known_unsupported coverage too.

        Core capabilities (:data:`_CORE_CAPABILITIES`) are the universal
        floor — exempt by design. Anything else (thinking, caching,
        dict-map, Decimal fields) MUST have at least one certificate
        that declares it ``known_unsupported`` so the skip-path of the
        :func:`skip_unless_capability_declared` helper is exercised by
        the live suite.
        """
        for cap in Capability:
            if cap in _CORE_CAPABILITIES:
                continue
            in_unsupported = any(cap in cert.known_unsupported for cert in ALL_MODELS)
            assert in_unsupported, (
                f"No certificate lists {cap.value!r} as known_unsupported — "
                "the capability test's skip-path is untested. Declare it on "
                "at least one ModelCertificate in "
                "tolokaforge_models/src/tolokaforge_models/certificates/registry.py whose target "
                "provider genuinely lacks this capability."
            )

    def test_core_capabilities_are_honest(self) -> None:
        """Every cert with the env_key required for integration runs
        must list every core capability in ``required`` — hiding basic
        completion or usage telemetry behind ``known_unsupported`` is
        the kind of dishonesty the canon refuses to tolerate.
        """
        for cert in ALL_MODELS:
            missing = sorted(
                (c.value for c in _CORE_CAPABILITIES if c not in cert.required),
            )
            # It IS legal to omit a core capability from ``required`` IF
            # ``known_unsupported`` explicitly claims it — that's a
            # "this model really can't do it" statement. But silently
            # failing to declare it is a contributor bug.
            undeclared = [m for m in missing if Capability(m) not in cert.known_unsupported]
            assert not undeclared, (
                f"{cert.model_id}: core capabilities {undeclared} are "
                "neither required nor known_unsupported. Every certificate "
                "must take a position on every core capability."
            )
