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
    """What the log path does with the vocabulary's answer.

    Which key names answer which way is locked once, in
    ``tests/unit/core/test_redaction.py`` — a second table here would go stale
    against the first rather than catch anything it misses.
    """

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
