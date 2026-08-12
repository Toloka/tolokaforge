"""Behaviour lock for the ``@register_probe`` / ``get_probe`` seam.

Locks the forward-compat dispatch surface out-of-tree probe bodies rely
on: the decorator installs into a module-level registry keyed by
``(capability, model_id)``; :func:`get_probe` prefers a model-specific
registration over the ``model_id=None`` default; a duplicate
registration fails loud with both offenders in the message.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tolokaforge.testing.certify import (
    Capability,
    ModelCertificate,
    get_probe,
    register_probe,
)
from tolokaforge.testing.certify.probes import (
    _clear_registry_for_tests,
    registered_probes,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    _clear_registry_for_tests()
    try:
        yield
    finally:
        _clear_registry_for_tests()


def _fake_cert(model_id: str) -> ModelCertificate:
    return ModelCertificate(
        model_id=model_id,
        provider="openrouter",
        name=model_id.replace("openrouter__", "").replace("_", "/"),
        env_key="OPENROUTER_API_KEY",
    )


class TestRegisterProbe:
    def test_decorator_returns_the_wrapped_callable(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def probe(cert: ModelCertificate) -> str:
            return f"basic-{cert.model_id}"

        assert callable(probe)
        assert probe(_fake_cert("openrouter__x_x")) == "basic-openrouter__x_x"

    def test_default_probe_lands_under_model_id_none(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def probe(cert: ModelCertificate) -> str:
            return "default"

        snapshot = registered_probes()
        assert (Capability.BASIC_COMPLETION, None) in snapshot
        assert snapshot[(Capability.BASIC_COMPLETION, None)] is probe

    def test_model_scoped_probe_lands_under_model_id(self) -> None:
        @register_probe(Capability.SIMPLE_TOOL_CALL, model_id="openrouter__foo_bar")
        def probe(cert: ModelCertificate) -> str:
            return "scoped"

        snapshot = registered_probes()
        assert (Capability.SIMPLE_TOOL_CALL, "openrouter__foo_bar") in snapshot

    def test_duplicate_registration_raises_naming_both_offenders(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def first(cert: ModelCertificate) -> str:
            return "first"

        with pytest.raises(RuntimeError) as exc:

            @register_probe(Capability.BASIC_COMPLETION)
            def second(cert: ModelCertificate) -> str:
                return "second"

        msg = str(exc.value)
        assert Capability.BASIC_COMPLETION.value in msg
        assert "None" in msg  # the default model_id key

    def test_duplicate_model_scoped_registration_raises(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION, model_id="openrouter__foo_bar")
        def first(cert: ModelCertificate) -> str:
            return "first"

        with pytest.raises(RuntimeError) as exc:

            @register_probe(Capability.BASIC_COMPLETION, model_id="openrouter__foo_bar")
            def second(cert: ModelCertificate) -> str:
                return "second"

        msg = str(exc.value)
        assert "openrouter__foo_bar" in msg


class TestGetProbe:
    def test_returns_none_when_registry_empty(self) -> None:
        assert get_probe(Capability.BASIC_COMPLETION, _fake_cert("openrouter__x_x")) is None

    def test_falls_back_to_default_when_no_model_specific_probe(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def default(cert: ModelCertificate) -> str:
            return "default"

        got = get_probe(Capability.BASIC_COMPLETION, _fake_cert("openrouter__any_any"))
        assert got is default

    def test_prefers_model_specific_probe_over_default(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def default(cert: ModelCertificate) -> str:
            return "default"

        @register_probe(Capability.BASIC_COMPLETION, model_id="openrouter__openai_gpt-5.4")
        def gpt54(cert: ModelCertificate) -> str:
            return "gpt54"

        gpt54_cert = _fake_cert("openrouter__openai_gpt-5.4")
        other_cert = _fake_cert("openrouter__anthropic_claude-opus-5")
        assert get_probe(Capability.BASIC_COMPLETION, gpt54_cert) is gpt54
        assert get_probe(Capability.BASIC_COMPLETION, other_cert) is default

    def test_returns_none_when_only_other_capabilitys_probe_registered(self) -> None:
        @register_probe(Capability.SIMPLE_TOOL_CALL)
        def unrelated(cert: ModelCertificate) -> str:
            return "unrelated"

        assert get_probe(Capability.BASIC_COMPLETION, _fake_cert("openrouter__x_x")) is None


class TestRegisteredProbes:
    def test_returns_a_snapshot_that_does_not_mutate_the_registry(self) -> None:
        @register_probe(Capability.BASIC_COMPLETION)
        def probe(cert: ModelCertificate) -> str:
            return "x"

        snapshot = registered_probes()
        assert isinstance(snapshot, dict)
        snapshot.clear()  # type: ignore[attr-defined]

        # Registry itself is unaffected.
        assert get_probe(Capability.BASIC_COMPLETION, _fake_cert("openrouter__x_x")) is probe
