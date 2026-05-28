"""Unit tests — :class:`Capability` enum + :class:`ModelCertificate` dataclass
(Stage 8 / P10 groundwork).

These tests pin the dataclass's honesty invariants, enum roundtrip
semantics, and per-certificate slug consistency with
:func:`tolokaforge.core.output.artifacts.model_id_slug`. They run as
plain unit tests (no live provider calls) so they catch contributor
errors in ``tests/integration/llm/registry.py`` at PR time — before a
live-provider sweep would even be attempted.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.integration.llm._capability import Capability, ModelCertificate
from tests.integration.llm.registry import ALL_MODELS
from tolokaforge.core.output.artifacts import model_id_slug

pytestmark = pytest.mark.unit


class TestCapabilityEnum:
    """:class:`Capability` is a string-valued enum — values must round-trip."""

    def test_round_trip_from_string_values(self) -> None:
        for cap in Capability:
            assert Capability(cap.value) is cap

    def test_enum_values_are_unique(self) -> None:
        values = [c.value for c in Capability]
        assert len(values) == len(set(values)), "Duplicate Capability enum values"

    def test_enum_is_str_subclass(self) -> None:
        # So tests can use capability.value interchangeably with the literal str.
        assert isinstance(Capability.BASIC_COMPLETION, str)


class TestModelCertificateConstruction:
    """Contract: required/known_unsupported are disjoint; frozen; equality works."""

    def test_minimal_construction(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-1",
            provider="openrouter",
            name="fake/fake-1",
            env_key="OPENROUTER_API_KEY",
        )
        assert cert.model_id == "openrouter__fake_fake-1"
        assert cert.required == frozenset()
        assert cert.known_unsupported == frozenset()

    def test_with_capability_sets(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-1",
            provider="openrouter",
            name="fake/fake-1",
            env_key="OPENROUTER_API_KEY",
            required=frozenset({Capability.BASIC_COMPLETION}),
            known_unsupported=frozenset({Capability.THINKING_EMITS_BLOCKS}),
        )
        assert Capability.BASIC_COMPLETION in cert.required
        assert Capability.THINKING_EMITS_BLOCKS in cert.known_unsupported

    def test_overlap_rejected(self) -> None:
        """A capability in both sets is dishonest — must raise."""
        with pytest.raises(ValueError, match="both `required` and `known_unsupported`"):
            ModelCertificate(
                model_id="openrouter__fake_fake-1",
                provider="openrouter",
                name="fake/fake-1",
                env_key="OPENROUTER_API_KEY",
                required=frozenset({Capability.BASIC_COMPLETION}),
                known_unsupported=frozenset({Capability.BASIC_COMPLETION}),
            )

    def test_multi_capability_overlap_lists_all(self) -> None:
        """The error message enumerates every overlapping capability."""
        with pytest.raises(ValueError) as exc:
            ModelCertificate(
                model_id="openrouter__fake_fake-1",
                provider="openrouter",
                name="fake/fake-1",
                env_key="OPENROUTER_API_KEY",
                required=frozenset({Capability.BASIC_COMPLETION, Capability.PROMPT_CACHING}),
                known_unsupported=frozenset(
                    {Capability.BASIC_COMPLETION, Capability.PROMPT_CACHING}
                ),
            )
        msg = str(exc.value)
        assert Capability.BASIC_COMPLETION.value in msg
        assert Capability.PROMPT_CACHING.value in msg

    def test_frozen_dataclass(self) -> None:
        cert = ModelCertificate(
            model_id="openrouter__fake_fake-1",
            provider="openrouter",
            name="fake/fake-1",
            env_key="OPENROUTER_API_KEY",
        )
        with pytest.raises(FrozenInstanceError):
            cert.model_id = "other"  # type: ignore[misc]


class TestRegistrySlugConsistency:
    """Every certificate in :data:`ALL_MODELS` must agree with
    :func:`tolokaforge.core.output.artifacts.model_id_slug`.

    This is the Stage 8 cross-module contract — Stage 7's per-trial
    ``tools_schemas/<task>__<model_id>.json`` sidecar filename uses the
    same slug, so any drift between the two would silently orphan the
    trial's schema snapshot from the capability registry.
    """

    def test_every_env_key_is_non_empty(self) -> None:
        for cert in ALL_MODELS:
            assert cert.env_key.strip(), f"{cert.model_id}: empty env_key"

    def test_slug_matches_model_id_slug(self) -> None:
        for cert in ALL_MODELS:
            expected = model_id_slug(cert.provider, cert.name)
            assert cert.model_id == expected, (
                f"Certificate model_id {cert.model_id!r} disagrees with "
                f"model_id_slug({cert.provider!r}, {cert.name!r}) = {expected!r}"
            )
