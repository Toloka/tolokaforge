"""Unit tests for :meth:`tolokaforge.core.logging.StructuredLogger._sanitize_extra`.

The helper is the single choke point for two policies:

1. Rename context keys that collide with ``LogRecord`` reserved attributes
   (``module``, ``name``, …) so ``Logger.log(..., extra=...)`` doesn't
   raise ``KeyError`` at record construction.
2. Redact values under keys naming a credential (``password``, ``secret``,
   ``token``, ``api_key``, …) so a caller passing sensitive data in the
   context dict cannot leak it into the log stream. The vocabulary is
   :mod:`tolokaforge.core.redaction`'s, shared with the artifact writer —
   these cases lock what the log path does with it.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.redaction import REDACTED_PLACEHOLDER

pytestmark = pytest.mark.unit


class TestReservedKeyRename:
    def test_reserved_keys_get_ctx_prefix(self) -> None:
        result = StructuredLogger._sanitize_extra({"module": "cli", "name": "run"})

        assert result == {"ctx_module": "cli", "ctx_name": "run"}

    def test_non_reserved_keys_are_left_alone(self) -> None:
        result = StructuredLogger._sanitize_extra({"user_id": 42, "trial": "a:0"})

        assert result == {"user_id": 42, "trial": "a:0"}


class TestSensitiveKeyRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "apikey",
            "API_KEY",
            "token",
            "access_token",
            "api_token",
            "authorization",
            "user_secret",
            "credential",
            "credentials",
            "openai_api_key",
        ],
    )
    def test_sensitive_key_value_is_redacted(self, key: str) -> None:
        result = StructuredLogger._sanitize_extra({key: "sk-abc123"})

        assert result[key] == REDACTED_PLACEHOLDER

    @pytest.mark.parametrize(
        "key",
        [
            "user_id",
            "trial_id",
            "model_name",
            "provider",
            "duration",
            "count",
            # Telemetry keys ending in `_tokens` are NOT credentials — the
            # `token`/`access_token` exact-form set redacts on word-boundary
            # matches only. Regression guard: if the redactor widens back
            # to substring `token`, these red.
            "max_tokens",
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
        ],
    )
    def test_non_sensitive_key_value_is_kept(self, key: str) -> None:
        result = StructuredLogger._sanitize_extra({key: 42})

        assert result[key] == 42

    def test_reserved_and_sensitive_combine_correctly(self) -> None:
        """A reserved-key rename followed by a sensitive-key match — the
        renamed key (``ctx_module``) is not sensitive, but a
        ``ctx_password`` from a hypothetical collision would still match.
        """
        result = StructuredLogger._sanitize_extra({"module": "cli", "password": "hunter2"})

        assert result == {"ctx_module": "cli", "password": REDACTED_PLACEHOLDER}

    def test_redaction_replaces_the_value_completely(self) -> None:
        """The redacted marker does not contain the original value."""
        result = StructuredLogger._sanitize_extra({"api_key": "sk-real-token-1234"})

        assert "sk-real-token" not in result["api_key"]
        assert result["api_key"] == REDACTED_PLACEHOLDER
