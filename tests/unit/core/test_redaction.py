"""Unit tests for :mod:`tolokaforge.core.redaction`.

Two contracts: the key-name vocabulary both redaction surfaces share, and the
recursion :class:`SensitiveKeyRedaction` applies over a mapping bound for disk.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.redaction import (
    REDACTED_PLACEHOLDER,
    NoRedaction,
    RedactionPolicyName,
    SensitiveKeyRedaction,
    key_is_sensitive,
)

pytestmark = pytest.mark.unit


class TestSensitiveKeyVocabulary:
    """The vocabulary as measured, not as the constants read.

    ``session_id`` sits in the exact-token set yet answers ``False``: the
    matcher tests whole non-alphanumeric-delimited parts, and neither
    ``session`` nor ``id`` is in either set. It is locked ``False`` because that
    is what the engine does today — #1158 is the repair, and until it lands a
    test claiming otherwise would be fiction.
    """

    @pytest.mark.parametrize(
        ("key", "sensitive"),
        [
            ("password", True),
            ("api_key", True),
            ("apikey", True),
            ("API_KEY", True),
            ("authorization", True),
            ("credential", True),
            ("user_secret", True),
            ("token", True),
            ("bearer", True),
            ("api_token", True),
            ("bearer_token", True),
            ("access_token", True),
            ("refresh_token", True),
            ("session_id", False),
            ("max_tokens", False),
            ("total_tokens", False),
            ("prompt_tokens", False),
            ("url", False),
            ("user_id", False),
        ],
    )
    def test_key_is_sensitive_matches_the_measured_table(self, key: str, sensitive: bool) -> None:
        assert key_is_sensitive(key) is sensitive


class TestSensitiveKeyRedaction:
    def test_credential_named_value_is_replaced(self) -> None:
        result = SensitiveKeyRedaction().redact_mapping({"api_token": "sk-live-abc123"})

        assert result == {"api_token": REDACTED_PLACEHOLDER}

    def test_ordinary_value_is_left_alone(self) -> None:
        result = SensitiveKeyRedaction().redact_mapping({"url": "https://api.example/v1"})

        assert result == {"url": "https://api.example/v1"}

    def test_nested_mapping_is_reached(self) -> None:
        result = SensitiveKeyRedaction().redact_mapping(
            {"body": {"api_key": "sk-live-abc123", "query": "orders"}}
        )

        assert result == {"body": {"api_key": REDACTED_PLACEHOLDER, "query": "orders"}}

    def test_mapping_inside_a_list_is_reached(self) -> None:
        result = SensitiveKeyRedaction().redact_mapping(
            {"headers": [{"api_key": "sk-live-abc123"}, {"accept": "application/json"}]}
        )

        assert result == {
            "headers": [{"api_key": REDACTED_PLACEHOLDER}, {"accept": "application/json"}]
        }

    def test_a_mapping_keyed_by_something_other_than_a_string_survives_the_walk(self) -> None:
        """An adapter's environment snapshot keys records by their integer id, and a
        key that is not a string names nothing — so the walk descends past it rather
        than asking the vocabulary about it, and still reaches what it holds."""
        result = SensitiveKeyRedaction().redact_mapping(
            {"orders": {1: {"api_token": "sk-live-abc123", "total": 328.5}, 2: {"total": 12.0}}}
        )

        assert result == {
            "orders": {1: {"api_token": REDACTED_PLACEHOLDER, "total": 328.5}, 2: {"total": 12.0}}
        }

    def test_the_input_mapping_is_not_mutated(self) -> None:
        """The writer redacts a trajectory the run still holds in memory."""
        arguments = {"body": {"api_key": "sk-live-abc123"}}

        SensitiveKeyRedaction().redact_mapping(arguments)

        assert arguments == {"body": {"api_key": "sk-live-abc123"}}


class TestNoRedaction:
    def test_credential_named_value_survives(self) -> None:
        result = NoRedaction().redact_mapping({"api_token": "sk-live-abc123"})

        assert result == {"api_token": "sk-live-abc123"}


class TestAPolicyCannotBeConstructedUnderAnotherPolicysName:
    """The one stamp a bundle's reader could not detect is a policy that rewrote
    the bundle while naming itself ``none``, so the name is not a settable field."""

    @pytest.mark.parametrize("policy", [NoRedaction, SensitiveKeyRedaction])
    def test_the_name_is_not_a_constructor_argument(self, policy: type) -> None:
        with pytest.raises(TypeError):
            policy(name=RedactionPolicyName.NONE)

    def test_each_policy_answers_with_its_own_name(self) -> None:
        assert NoRedaction().name is RedactionPolicyName.NONE
        assert SensitiveKeyRedaction().name is RedactionPolicyName.SENSITIVE_KEYS
