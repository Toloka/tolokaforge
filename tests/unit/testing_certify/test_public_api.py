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
            "get_probe",
            "register_probe",
        }

    def test_fixtures_are_not_reachable_from_public_namespace(self) -> None:
        # Importing the package must not pull in pytest — the fixtures live
        # on the submodule so a runtime caller of the certify seam can use
        # it without installing pytest.
        assert not hasattr(certify, "live_client")
        assert not hasattr(certify, "skip_unless_capability_declared")

    def test_fixtures_reachable_via_submodule(self) -> None:
        # Suite authors reach the fixtures through the submodule — the same
        # object ``pytest_plugins = ["tolokaforge.testing.certify.fixtures"]``
        # loads into the pytest session.
        from tolokaforge.testing.certify import fixtures

        assert callable(fixtures.live_client)
        assert callable(fixtures.skip_unless_capability_declared)

    def test_probe_seam_is_reachable_from_public_namespace(self) -> None:
        assert callable(certify.register_probe)
        assert callable(certify.get_probe)


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
        # Any accidental add/drop of a Capability trips this guard
        # before the canonical registry test sees the change.
        # 24 = the 23 pre-#1278 capabilities + REASONING_EFFORT_HONOURED.
        assert len(list(Capability)) == 24
        assert Capability.REASONING_EFFORT_HONOURED in set(Capability)


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

    def test_required_and_excluded_capabilities_overlap_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            ModelCertificate(
                model_id="openrouter__fake_fake-7",
                provider="openrouter",
                name="fake/fake-7",
                env_key="OPENROUTER_API_KEY",
                required=frozenset({Capability.PROMPT_CACHING, Capability.BASIC_COMPLETION}),
                excluded_capabilities=frozenset(
                    {Capability.PROMPT_CACHING, Capability.BASIC_COMPLETION}
                ),
            )
        msg = str(exc.value)
        assert "excluded_capabilities" in msg
        assert Capability.PROMPT_CACHING.value in msg
        assert Capability.BASIC_COMPLETION.value in msg

    def test_known_unsupported_and_excluded_may_overlap(self) -> None:
        # muse-spark's shape: declare honestly (known_unsupported) AND opt
        # out of the ratchet (excluded_capabilities). Both together is the
        # intended pattern, not an invariant violation.
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-8",
            provider="openrouter",
            name="fake/fake-8",
            env_key="OPENROUTER_API_KEY",
            known_unsupported=frozenset({Capability.IMPLICIT_PROMPT_CACHING}),
            excluded_capabilities=frozenset({Capability.IMPLICIT_PROMPT_CACHING}),
        )
        assert Capability.IMPLICIT_PROMPT_CACHING in cert.known_unsupported
        assert Capability.IMPLICIT_PROMPT_CACHING in cert.excluded_capabilities


class TestHashable:
    def _make_cert(self, **overrides: object) -> ModelCertificate:
        defaults: dict[str, object] = {
            "model_id": "openrouter__fake_fake-hash",
            "provider": "openrouter",
            "name": "fake/fake-hash",
            "env_key": "OPENROUTER_API_KEY",
            "required": frozenset({Capability.BASIC_COMPLETION}),
            "known_unsupported": frozenset({Capability.IMPLICIT_PROMPT_CACHING}),
            "excluded_capabilities": frozenset({Capability.IMPLICIT_PROMPT_CACHING}),
            "known_unsupported_reasons": {Capability.IMPLICIT_PROMPT_CACHING: "unreliable"},
            "probe_params": {Capability.PROMPT_CACHING: {"prompt_tokens": 12000}},
            "capability_extras": {"quirk": "wrap"},
        }
        defaults.update(overrides)
        return ModelCertificate(**defaults)  # type: ignore[arg-type]

    def test_populated_cert_is_hashable(self) -> None:
        cert = self._make_cert()
        assert isinstance(hash(cert), int)

    def test_equal_populated_certs_hash_equal(self) -> None:
        a = self._make_cert()
        b = self._make_cert()
        assert a == b
        assert hash(a) == hash(b)

    def test_certs_differing_on_excluded_hash_differently(self) -> None:
        a = self._make_cert()
        c = self._make_cert(excluded_capabilities=frozenset())
        assert a != c
        assert hash(a) != hash(c)

    def test_registry_certs_all_hashable(self) -> None:
        # Guards against a registry entry with mapping data that would
        # break the hash contract in production.
        {hash(cert) for cert in ALL_MODELS}


class TestMappingFieldsFrozen:
    def test_mapping_fields_are_read_only(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-frozen",
            provider="openrouter",
            name="fake/fake-frozen",
            env_key="OPENROUTER_API_KEY",
            known_unsupported_reasons={Capability.PROMPT_CACHING: "reason"},
            probe_params={Capability.PROMPT_CACHING: {"prompt_tokens": 12000}},
            capability_extras={"quirk": "wrap"},
        )
        with pytest.raises(TypeError):
            cert.known_unsupported_reasons[Capability.BASIC_COMPLETION] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            cert.probe_params[Capability.BASIC_COMPLETION] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            cert.capability_extras["k"] = "v"  # type: ignore[index]

    def test_mutating_source_dict_does_not_bleed_into_cert(self) -> None:
        source = {Capability.PROMPT_CACHING: "reason"}
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-bleed",
            provider="openrouter",
            name="fake/fake-bleed",
            env_key="OPENROUTER_API_KEY",
            known_unsupported_reasons=source,
        )
        source[Capability.BASIC_COMPLETION] = "leaked"
        assert Capability.BASIC_COMPLETION not in cert.known_unsupported_reasons


class TestAllModels:
    def test_non_empty_tuple(self) -> None:
        assert isinstance(ALL_MODELS, tuple)
        assert len(ALL_MODELS) > 0

    def test_repeated_import_preserves_order(self) -> None:
        from tolokaforge_models.certificates import registry

        first = [c.model_id for c in ALL_MODELS]
        reloaded = [c.model_id for c in importlib.reload(registry).ALL_MODELS]
        assert reloaded == first

    def test_certify_seam_and_models_wheel_return_the_same_tuple(self) -> None:
        # The engine re-export must BE the wheel's tuple, not a copy that can
        # drift. Kept from the deleted cutover lock; see ADR-0030.
        from tolokaforge_models.certificates import ALL_MODELS as WHEEL_ALL_MODELS

        assert ALL_MODELS is WHEEL_ALL_MODELS
