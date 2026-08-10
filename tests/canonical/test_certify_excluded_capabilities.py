"""Behaviour lock — data-borne exclusions and ratchet-target invariants.

Two invariants ride together here and neither can drift silently:

1. The muse-spark-1.1 cert opts out of the implicit-prompt-caching
   ratchet through :attr:`ModelCertificate.excluded_capabilities` —
   the exclusion is certificate data, not a hardcoded name-set inside
   the ratchet module.
2. Every ``_ratchet_targets()`` in the suite returns the baseline
   ``model_id`` set embedded below. Changing the exclusion predicate
   shape must not perturb the set of model_ids it resolves to.
"""

from __future__ import annotations

import importlib

import pytest

from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

pytestmark = pytest.mark.canonical

_RATCHET_MODULES = (
    "tolokaforge.testing.certify.suite.test_implicit_prompt_caching_unsupported_ratchet",
    "tolokaforge.testing.certify.suite.test_re2_pattern_tolerance_unsupported_ratchet",
    "tolokaforge.testing.certify.suite.test_enum_slash_tolerance_unsupported_ratchet",
)

# Baseline captured against the ratchet's current predicate. A diff
# here means the certificate-data lookup resolves to a different
# target set than expected — investigate before regenerating.
_RATCHET_TARGETS_BASELINE: dict[str, list[str]] = {
    "tolokaforge.testing.certify.suite.test_implicit_prompt_caching_unsupported_ratchet": [
        "openrouter__deepseek_deepseek-v3.2-exp",
        "openrouter__deepseek_deepseek-v4-flash",
        "openrouter__deepseek_deepseek-v4-pro",
        "openrouter__google_gemini-3-flash-preview",
        "openrouter__google_gemini-3.1-pro-preview",
        "openrouter__google_gemini-3.5-flash",
        "openrouter__google_gemini-3.6-flash",
        "openrouter__google_gemma-4-31b-it",
        "openrouter__mistralai_mistral-medium-3-5",
        "openrouter__moonshotai_kimi-k2.6",
        "openrouter__moonshotai_kimi-k2.7-code",
        "openrouter__nvidia_nemotron-3-super-120b-a12b",
        "openrouter__nvidia_nemotron-3-ultra-550b-a55b",
        "openrouter__openai_gpt-5.6-sol",
        "openrouter__openai_gpt-5.6-terra",
        "openrouter__openai_gpt-oss-120b",
        "openrouter__qwen_qwen3.6-plus",
        "openrouter__tencent_hy3-preview",
        "openrouter__x-ai_grok-4",
        "openrouter__x-ai_grok-4.3",
        "openrouter__x-ai_grok-4.5",
    ],
    "tolokaforge.testing.certify.suite.test_re2_pattern_tolerance_unsupported_ratchet": [
        "openrouter__openai_gpt-5.6-sol",
        "openrouter__openai_gpt-5.6-terra",
        "openrouter__x-ai_grok-4.3",
    ],
    "tolokaforge.testing.certify.suite.test_enum_slash_tolerance_unsupported_ratchet": [
        "openrouter__x-ai_grok-4.3",
    ],
}


def _muse_spark_cert() -> ModelCertificate:
    for cert in ALL_MODELS:
        if cert.model_id == "openrouter__meta_muse-spark-1.1":
            return cert
    raise AssertionError("muse-spark-1.1 certificate missing from ALL_MODELS")


class TestExclusionMovedFromHardcodedSetToCertificate:
    def test_muse_spark_declares_implicit_caching_excluded(self) -> None:
        cert = _muse_spark_cert()
        assert Capability.IMPLICIT_PROMPT_CACHING in cert.excluded_capabilities

    def test_muse_spark_still_declares_implicit_caching_known_unsupported(self) -> None:
        # The exclusion is orthogonal to the honesty declaration —
        # excluding from the ratchet does not un-declare the capability
        # as known-unsupported.
        cert = _muse_spark_cert()
        assert Capability.IMPLICIT_PROMPT_CACHING in cert.known_unsupported

    def test_ratchet_module_has_no_legacy_hardcoded_exclusion_set(self) -> None:
        module = importlib.import_module(
            "tolokaforge.testing.certify.suite.test_implicit_prompt_caching_unsupported_ratchet",
        )
        assert not hasattr(module, "_UNRELIABLE_COLD_CACHE_REPORT_NAMES")

    def test_ratchet_excludes_muse_spark(self) -> None:
        module = importlib.import_module(
            "tolokaforge.testing.certify.suite.test_implicit_prompt_caching_unsupported_ratchet",
        )
        targets = module._ratchet_targets()
        target_ids = {cert.model_id for cert in targets}
        assert "openrouter__meta_muse-spark-1.1" not in target_ids


class TestRatchetTargetsSetForSet:
    """Baseline lock — ``excluded_capabilities`` must not perturb the
    computed target set of any ratchet in the suite.
    """

    @pytest.mark.parametrize("module_name", _RATCHET_MODULES)
    def test_ratchet_targets_match_baseline(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        actual = sorted(cert.model_id for cert in module._ratchet_targets())
        expected = _RATCHET_TARGETS_BASELINE[module_name]
        assert actual == expected


class TestExcludedCapabilitiesRoundTrip:
    def test_field_survives_construction(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake",
            provider="openrouter",
            name="fake/fake",
            env_key="OPENROUTER_API_KEY",
            excluded_capabilities=frozenset(
                {Capability.IMPLICIT_PROMPT_CACHING, Capability.PROMPT_CACHING}
            ),
        )
        assert cert.excluded_capabilities == frozenset(
            {Capability.IMPLICIT_PROMPT_CACHING, Capability.PROMPT_CACHING}
        )
