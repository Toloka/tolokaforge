"""Behaviour-locking tests for ``HarnessSpec.credential_gateway``.

Locks two things: every shipped harness declares a valid
``CredentialGateway`` block, and that block's field names stay structurally
compatible with ``LLMGatewayEndpoint``'s ``CredentialGatewayConfig``
Protocol (``tolokaforge/core/drivers/llm_gateway.py``) — the seam Stage 1
shipped ahead of this model existing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.drivers.llm_gateway import CredentialGatewayConfig
from tolokaforge_coding_harnesses import HARNESSES, CredentialGateway

pytestmark = pytest.mark.unit


class TestEveryShippedHarnessHasACredentialGateway:
    @pytest.mark.parametrize("harness_name", sorted(HARNESSES))
    def test_credential_gateway_is_populated(self, harness_name: str) -> None:
        spec = HARNESSES[harness_name]
        assert spec.credential_gateway is not None, (
            f"{harness_name!r} ships no credential_gateway — every shipped harness "
            "must shield its real provider credential from the trial container."
        )


class TestCredentialGatewayRejectsUnknownFields:
    def test_extra_field_is_refused(self) -> None:
        base = HARNESSES["claude-code"].credential_gateway
        assert base is not None
        payload = base.model_dump()
        payload["not_a_real_field"] = "value"
        with pytest.raises(ValidationError):
            CredentialGateway.model_validate(payload)


class TestStructuralFitWithLLMGatewayEndpoint:
    def test_protocol_fields_are_a_subset_of_the_model_fields(self) -> None:
        """Every field ``LLMGatewayEndpoint`` reads off ``spec.credential_gateway``
        exists on ``CredentialGateway`` with the same name.

        Not full set equality: ``CredentialGateway`` legitimately carries
        fields the gateway endpoint itself never reads
        (``dummy_token_env_var``, ``dummy_token_value``, ``base_url_env_var``
        — consumed by the driver's compose wiring instead). The invariant
        this test locks is that a real ``CredentialGateway`` instance always
        satisfies ``CredentialGatewayConfig`` structurally, which only
        requires the protocol's names to be present, not exclusive.
        """
        protocol_fields = set(CredentialGatewayConfig.__annotations__)
        model_fields = set(CredentialGateway.model_fields)
        missing = protocol_fields - model_fields
        assert not missing, (
            f"CredentialGateway is missing field(s) {sorted(missing)!r} that "
            "CredentialGatewayConfig requires; LLMGatewayEndpoint's structural "
            "typing depends on every protocol field existing under the same name."
        )

    def test_a_real_credential_gateway_instance_carries_every_protocol_attribute(self) -> None:
        """``CredentialGatewayConfig`` is a plain (non-``runtime_checkable``)
        Protocol, so this checks attribute presence directly rather than
        via ``isinstance`` — the same guarantee an ``isinstance`` check
        against a runtime-checkable Protocol would give (attribute
        presence, not type conformance)."""
        spec = HARNESSES["claude-code"].credential_gateway
        assert spec is not None
        for field in CredentialGatewayConfig.__annotations__:
            assert hasattr(spec, field), f"{spec!r} is missing protocol attribute {field!r}"


class TestPathAllowlistDefault:
    def test_default_has_exactly_the_four_documented_paths(self) -> None:
        assert CredentialGateway.model_fields["path_allowlist"].default == (
            "/v1/messages",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/models",
        )
