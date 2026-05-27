"""Tests for the runner's `_bootstrap_secrets_from_env` helper.

The helper extracts the inline TOLOKAFORGE_SECRETS_JSON parsing block from
`run_server()` so the failure branch is unit-testable. The malformed-payload
test pins down the new warning shape that satisfies Semgrep's
`logger-credential-leak` rule (no SECRETS keyword in the message, no raw
payload) while still surfacing line/col/parser-msg for debugging.
"""

from __future__ import annotations

import logging

import pytest

from tolokaforge.runner.__main__ import _bootstrap_secrets_from_env
from tolokaforge.secrets import SecretManager
from tolokaforge.secrets import manager as manager_module

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    original = manager_module._default_manager
    manager_module._default_manager = None
    yield
    manager_module._default_manager = original


def _logger() -> logging.Logger:
    return logging.getLogger("tolokaforge.runner.__main__")


def test_empty_payload_returns_none_and_logs_lazy_init(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_logger().name):
        result = _bootstrap_secrets_from_env("", _logger())

    assert result is None
    assert manager_module._default_manager is None
    info_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("lazy-init" in m for m in info_messages), info_messages


def test_valid_payload_returns_populated_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    payload = '{"OPENROUTER_API_KEY":"v123longvalue"}'

    result = _bootstrap_secrets_from_env(payload, _logger())

    assert isinstance(result, SecretManager)
    assert result.get_secret("OPENROUTER_API_KEY") == "v123longvalue"
    # Singleton must be installed (so later get_default() callers see this manager).
    assert manager_module._default_manager is result


def test_non_object_payload_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Valid JSON, but not a dict — must be rejected without crashing.
    payload = '"just a string"'

    with caplog.at_level(logging.WARNING, logger=_logger().name):
        result = _bootstrap_secrets_from_env(payload, _logger())

    assert result is None
    assert manager_module._default_manager is None
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_messages, "expected a warning for non-object payload"


def test_malformed_payload_warning_does_not_leak_secrets_keyword_or_doc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pin the warning shape that satisfies Semgrep `logger-credential-leak`.

    The message must NOT contain the env-var name `TOLOKAFORGE_SECRETS_JSON`
    (the keyword that triggered the rule) and must NOT contain the raw
    payload (which carries credentials). It must contain enough structural
    info (line/col + the parser's own msg) to debug a bad payload.
    """
    payload = '{"openai":'

    with caplog.at_level(logging.WARNING, logger=_logger().name):
        result = _bootstrap_secrets_from_env(payload, _logger())

    assert result is None
    assert manager_module._default_manager is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    msg = warnings[0].getMessage()

    assert "TOLOKAFORGE_SECRETS_JSON" not in msg
    assert payload not in msg
    assert "openai" not in msg  # raw doc must not leak via any field
    assert "line" in msg.lower()
    assert "col" in msg.lower()
    # The parser's own message ("Expecting value", "Expecting property name…", …)
    # is fixed CPython text and never contains the input — must be present.
    assert any(token in msg for token in ("Expecting", "Unterminated"))
