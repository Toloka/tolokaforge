"""Tests for registering a credential the process resolved at runtime.

The contract is that a registered key is indistinguishable from a ``.env`` key
to every consumer of the default SecretManager — resolution, transport and log
redaction alike.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest

from tolokaforge.secrets import (
    DotEnvProvider,
    EnvProvider,
    RuntimeSecretConflictError,
    RuntimeSecretProvider,
    SecretManager,
    get_default,
    init_default_from,
    register_runtime_secret,
)
from tolokaforge.secrets import log_filter as log_filter_module
from tolokaforge.secrets import manager as manager_module
from tolokaforge.secrets.log_filter import PLACEHOLDER, install_global_redactor

pytestmark = pytest.mark.unit

GENERATED = "GENERATEDKEY0123456789"


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh singleton and a cold redaction cache."""
    saved_manager = manager_module._default_manager
    saved_cached_manager = log_filter_module._cached_manager
    saved_cached_values = log_filter_module._cached_values
    manager_module._default_manager = None
    log_filter_module._cached_manager = None
    log_filter_module._cached_values = frozenset()
    yield
    manager_module._default_manager = saved_manager
    log_filter_module._cached_manager = saved_cached_manager
    log_filter_module._cached_values = saved_cached_values


@pytest.fixture
def restore_log_record_factory():
    """Reset the LogRecord factory to vanilla for the test, then restore.

    Other test modules transitively import ``tolokaforge.dx.cli.main``, which
    calls ``install_global_redactor()`` at module-import time; forcing a
    baseline keeps installation order-independent.
    """
    original = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


def _capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """An isolated logger whose records are captured in a buffer."""
    buf = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(logging.StreamHandler(buf))
    logger.setLevel(logging.INFO)
    return logger, buf


def _dotenv_manager(tmp_path: Path, body: str) -> SecretManager:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return SecretManager([DotEnvProvider(env_file), EnvProvider()])


def test_a_value_registered_after_the_redactor_warmed_its_cache_is_redacted(
    restore_log_record_factory,
) -> None:
    """Production ordering: the run logs before the credential exists.

    ``_ensure_typesense_started`` emits "Starting local TypeSense server" before
    it generates the API key, so the redactor's identity-keyed value cache is
    already warm at registration time. Registering must still redact — a
    mechanism that mutates the live manager leaves that warm cache blind.
    """
    init_default_from(SecretManager.from_dict({"OPENROUTER_API_KEY": "sk-existing-value"}))
    install_global_redactor()
    logger, buf = _capturing_logger("tolokaforge.test.runtime_registration_ordering")

    warmed_against = get_default()
    logger.info("Starting local TypeSense server")
    warmed_cache = log_filter_module._cached_manager
    assert warmed_cache is warmed_against, "the pre-registration record did not warm the cache"

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)
    logger.info("Creating adapter params=%s", {"typesense": {"api_key": GENERATED}})
    for handler in logger.handlers:
        handler.flush()

    output = buf.getvalue()
    assert GENERATED not in output
    assert PLACEHOLDER in output


def test_a_registered_key_resolves_and_travels_in_the_serialized_payload(
    tmp_path: Path,
) -> None:
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    assert get_default().get_secret("TYPESENSE_API_KEY") == GENERATED
    assert get_default().serialize()["TYPESENSE_API_KEY"] == GENERATED
    assert "TYPESENSE_API_KEY" in get_default().list_all_keys()


def test_a_registered_key_does_not_reach_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    assert "TYPESENSE_API_KEY" not in os.environ


def test_the_configured_chain_still_resolves_after_registration(tmp_path: Path) -> None:
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))
    assert get_default().get_secret("OPENROUTER_API_KEY") == "sk-from-dotenv"

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    assert get_default().get_secret("OPENROUTER_API_KEY") == "sk-from-dotenv"
    assert get_default().get_secret("TYPESENSE_API_KEY") == GENERATED


def test_a_runtime_value_wins_over_a_stale_entry_of_the_same_name(tmp_path: Path) -> None:
    """The registered provider goes ahead of the chain, not behind it."""
    init_default_from(_dotenv_manager(tmp_path, "TYPESENSE_API_KEY=stale-from-dotenv\n"))
    assert get_default().get_secret("TYPESENSE_API_KEY") == "stale-from-dotenv"

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    assert get_default().get_secret("TYPESENSE_API_KEY") == GENERATED


def test_registering_a_second_key_keeps_one_runtime_provider(tmp_path: Path) -> None:
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))

    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)
    register_runtime_secret("OTHER_RUNTIME_TOKEN", "second-runtime-value")

    chain = get_default().providers
    assert [type(p) for p in chain] == [RuntimeSecretProvider, DotEnvProvider, EnvProvider]
    assert get_default().get_secret("TYPESENSE_API_KEY") == GENERATED
    assert get_default().get_secret("OTHER_RUNTIME_TOKEN") == "second-runtime-value"


def test_re_registering_the_identical_value_is_a_no_op(tmp_path: Path) -> None:
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))

    first = register_runtime_secret("TYPESENSE_API_KEY", GENERATED)
    second = register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    assert second is first
    assert get_default() is first
    assert len(get_default().providers) == 3


def test_re_registering_a_different_value_raises(tmp_path: Path) -> None:
    init_default_from(_dotenv_manager(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n"))
    register_runtime_secret("TYPESENSE_API_KEY", GENERATED)

    with pytest.raises(RuntimeSecretConflictError) as excinfo:
        register_runtime_secret("TYPESENSE_API_KEY", "A-SECOND-SERVER-KEY")

    assert excinfo.value.key == "TYPESENSE_API_KEY"
    assert get_default().get_secret("TYPESENSE_API_KEY") == GENERATED


def test_registering_an_empty_value_raises(tmp_path: Path) -> None:
    """An empty runtime value would shadow the configured chain with nothing."""
    init_default_from(_dotenv_manager(tmp_path, "TYPESENSE_API_KEY=real-from-dotenv\n"))

    with pytest.raises(ValueError, match="TYPESENSE_API_KEY"):
        register_runtime_secret("TYPESENSE_API_KEY", "")

    assert get_default().get_secret("TYPESENSE_API_KEY") == "real-from-dotenv"
