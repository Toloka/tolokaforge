"""Unit tests for :func:`tolokaforge.core.llm.providers.credential_env_names`.

Locks the provider -> expected ``SecretManager`` key-name mapping that
``jury_rubric``'s panel credential preflight reads.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.providers import (
    _CREDENTIAL_ENV_NAME_FALLBACKS,
    CLI_EXPORTED_CREDENTIAL_ENV_NAMES,
    credential_env_names,
)

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


def test_fallback_providers_all_covered_by_cli_export_list() -> None:
    """Every env-var name the fallbacks resolve to must appear in the CLI export list.

    The CLI mirrors ``CLI_EXPORTED_CREDENTIAL_ENV_NAMES`` into ``os.environ``
    at startup so litellm can authenticate. If a new provider is added to
    ``_CREDENTIAL_ENV_NAME_FALLBACKS`` without updating the CLI list, jury's
    per-provider preflight would silently pass while the runtime call
    later fails on a missing env var. Lock the two lists in a single test.
    """
    exported = set(CLI_EXPORTED_CREDENTIAL_ENV_NAMES)
    for provider, names in _CREDENTIAL_ENV_NAME_FALLBACKS.items():
        missing = [n for n in names if n not in exported]
        assert not missing, (
            f"provider {provider!r} needs env var(s) {missing} that are not in "
            f"CLI_EXPORTED_CREDENTIAL_ENV_NAMES; the CLI startup mirror will "
            f"not populate them, so preflight would silently pass but the "
            f"real litellm call would fail on a missing env var."
        )
