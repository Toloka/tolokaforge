"""Public API contract for :mod:`tolokaforge.testing.certify`.

Locks:

* The exported ``__all__`` — the seam every out-of-tree caller relies
  on. Additions extend this set as later stages introduce new
  surface (probe registration).
* :class:`Capability` shape — string-valued ``Enum``, unique values,
  round-trip via the value string.
* :class:`ModelCertificate` construction — the five originally-required
  fields succeed alone, the four widened fields default to empty
  containers, the frozen contract holds, and the overlap invariant
  raises ``ValueError`` with every offending capability listed.
* :data:`ALL_MODELS` shape — non-empty tuple; repeated import yields
  identical ordering (pytest collection determinism guard, sibling to
  the canonical registry test's deterministic-order assertion).
"""

from __future__ import annotations

import importlib
from dataclasses import FrozenInstanceError

import pytest

import tolokaforge.testing.certify as certify
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

pytestmark = pytest.mark.unit


class TestPublicAll:
    def test_all_exports_public_surface(self) -> None:
        assert set(certify.__all__) == {
            "ALL_MODELS",
            "Capability",
            "ModelCertificate",
            "live_client",
            "skip_unless_capability_declared",
        }

    def test_fixtures_are_reachable_from_public_namespace(self) -> None:
        # Same objects the suite conftest re-exports via pytest_plugins.
        assert callable(certify.live_client)
        assert callable(certify.skip_unless_capability_declared)


class TestCapabilityShape:
    def test_capability_is_str_enum(self) -> None:
        assert issubclass(Capability, str)
        assert isinstance(Capability.BASIC_COMPLETION, str)

    def test_capability_values_round_trip(self) -> None:
        for cap in Capability:
            assert Capability(cap.value) is cap

    def test_capability_values_are_unique(self) -> None:
        values = [c.value for c in Capability]
        assert len(values) == len(set(values))

    def test_capability_membership_stable(self) -> None:
        # Stage 1 is a verbatim relocation — the enum stays at its current
        # 23 members. Any accidental add/drop trips this guard before the
        # canonical registry test sees the change.
        assert len(list(Capability)) == 23


class TestModelCertificateConstruction:
    def test_minimal_construction_defaults_widened_fields(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-1",
            provider="openrouter",
            name="fake/fake-1",
            env_key="OPENROUTER_API_KEY",
        )
        assert cert.required == frozenset()
        assert cert.known_unsupported == frozenset()
        assert cert.excluded_capabilities == frozenset()
        assert cert.known_unsupported_reasons == {}
        assert cert.probe_params == {}
        assert cert.capability_extras == {}

    def test_widened_fields_accept_populated_values(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-2",
            provider="openrouter",
            name="fake/fake-2",
            env_key="OPENROUTER_API_KEY",
            excluded_capabilities=frozenset({Capability.IMPLICIT_PROMPT_CACHING}),
            known_unsupported_reasons={Capability.THINKING_EMITS_BLOCKS: "no signed blocks"},
            probe_params={Capability.PROMPT_CACHING: {"prompt_tokens": 12000}},
            capability_extras={"quirk": "wrap"},
        )
        assert Capability.IMPLICIT_PROMPT_CACHING in cert.excluded_capabilities
        assert (
            cert.known_unsupported_reasons[Capability.THINKING_EMITS_BLOCKS] == "no signed blocks"
        )
        assert cert.probe_params[Capability.PROMPT_CACHING] == {"prompt_tokens": 12000}
        assert cert.capability_extras["quirk"] == "wrap"

    def test_frozen(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-3",
            provider="openrouter",
            name="fake/fake-3",
            env_key="OPENROUTER_API_KEY",
        )
        with pytest.raises(FrozenInstanceError):
            cert.model_id = "other"  # type: ignore[misc]

    def test_default_containers_are_not_shared(self) -> None:
        first = ModelCertificate(
            model_id="openrouter__fake_fake-4",
            provider="openrouter",
            name="fake/fake-4",
            env_key="OPENROUTER_API_KEY",
        )
        second = ModelCertificate(
            model_id="openrouter__fake_fake-5",
            provider="openrouter",
            name="fake/fake-5",
            env_key="OPENROUTER_API_KEY",
        )
        # Each instance carries its own default container (defensive against
        # a mutable-default regression).
        assert first.known_unsupported_reasons is not second.known_unsupported_reasons
        assert first.probe_params is not second.probe_params
        assert first.capability_extras is not second.capability_extras


class TestOverlapInvariant:
    def test_overlap_raises_and_names_every_offender(self) -> None:
        with pytest.raises(ValueError) as exc:
            ModelCertificate(
                model_id="openrouter__fake_fake-6",
                provider="openrouter",
                name="fake/fake-6",
                env_key="OPENROUTER_API_KEY",
                required=frozenset({Capability.BASIC_COMPLETION, Capability.PROMPT_CACHING}),
                known_unsupported=frozenset(
                    {Capability.BASIC_COMPLETION, Capability.PROMPT_CACHING}
                ),
            )
        msg = str(exc.value)
        assert Capability.BASIC_COMPLETION.value in msg
        assert Capability.PROMPT_CACHING.value in msg


class TestAllModels:
    def test_non_empty_tuple(self) -> None:
        assert isinstance(ALL_MODELS, tuple)
        assert len(ALL_MODELS) > 0

    def test_repeated_import_preserves_order(self) -> None:
        from tolokaforge.testing.certify import _registry

        first = [c.model_id for c in ALL_MODELS]
        reloaded = [c.model_id for c in importlib.reload(_registry).ALL_MODELS]
        assert reloaded == first
