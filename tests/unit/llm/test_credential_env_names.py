"""Unit tests for :func:`tolokaforge.core.llm.providers.credential_env_names`.

Locks the provider -> expected ``SecretManager`` key-name mapping that
``jury_rubric``'s panel credential preflight (issue #1602) will read.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.providers import credential_env_names

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openrouter", ("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY")),
        ("nova", ("NOVA_API_KEY",)),
        ("openai", ("OPENAI_API_KEY",)),
        ("anthropic", ("ANTHROPIC_API_KEY",)),
        ("gemini", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
        ("google", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
    ],
)
def test_known_providers_resolve_expected_names(provider: str, expected: tuple[str, ...]) -> None:
    assert credential_env_names(provider) == expected


def test_segment_split_matches_get_provider_binding() -> None:
    """``"openrouter/google"`` resolves the same as bare ``"openrouter"``."""
    assert credential_env_names("openrouter/google") == credential_env_names("openrouter")


def test_unrecognised_provider_returns_empty_tuple() -> None:
    assert credential_env_names("totally_unknown") == ()


def test_mock_provider_has_no_credential_to_validate() -> None:
    """``mock``'s binding has every field at its inert default — nothing to check."""
    assert credential_env_names("mock") == ()
