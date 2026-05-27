"""Tests for SecretManager singleton + DictProvider + serialize/from_dict round-trip."""

from __future__ import annotations

import os

import pytest

from tolokaforge.secrets import (
    DictProvider,
    SecretConfig,
    SecretManager,
    SecretSource,
    get_default,
    init_default,
    init_default_from,
)
from tolokaforge.secrets import manager as manager_module

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh singleton — no leakage between cases."""
    original = manager_module._default_manager
    manager_module._default_manager = None
    yield
    manager_module._default_manager = original


def test_init_default_returns_same_instance_on_second_call():
    a = init_default(SecretConfig(sources=[SecretSource.ENV]))
    b = init_default(SecretConfig(sources=[SecretSource.ENV]))
    assert a is b


def test_get_default_auto_initializes_with_default_config():
    sm = get_default()
    assert isinstance(sm, SecretManager)


def test_init_default_from_replaces_singleton():
    custom = SecretManager.from_dict({"X": "1"})
    init_default_from(custom)
    assert get_default() is custom


def test_dict_provider_round_trip():
    secrets = {"OPENROUTER_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "ak-test"}
    sm = SecretManager.from_dict(secrets)
    assert sm.get_secret("OPENROUTER_API_KEY") == "sk-test"
    assert sm.get_secret("ANTHROPIC_API_KEY") == "ak-test"
    assert sm.get_secret("MISSING") is None
    assert sorted(sm.list_all_keys()) == ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]


def test_serialize_round_trip_through_dict_provider():
    secrets = {"OPENROUTER_API_KEY": "sk-1", "TYPESENSE_API_KEY": "ts-1"}
    sm_a = SecretManager([DictProvider(secrets)])
    payload = sm_a.serialize()
    assert payload == secrets

    sm_b = SecretManager.from_dict(payload)
    assert sm_b.get_secret("OPENROUTER_API_KEY") == "sk-1"
    assert sm_b.get_secret("TYPESENSE_API_KEY") == "ts-1"


def test_export_to_environ_uses_setdefault(monkeypatch: pytest.MonkeyPatch):
    """Existing env vars must NOT be overwritten."""
    monkeypatch.delenv("FAKE_NEW_KEY", raising=False)
    monkeypatch.setenv("FAKE_EXISTING_KEY", "from-shell")

    sm = SecretManager.from_dict(
        {"FAKE_NEW_KEY": "from-secret", "FAKE_EXISTING_KEY": "from-secret"}
    )
    exported = sm.export_to_environ(["FAKE_NEW_KEY", "FAKE_EXISTING_KEY"])
    assert exported == 2

    assert os.environ["FAKE_NEW_KEY"] == "from-secret"
    # Existing shell value wins:
    assert os.environ["FAKE_EXISTING_KEY"] == "from-shell"


def test_export_to_environ_skips_missing_secrets():
    sm = SecretManager.from_dict({})
    exported = sm.export_to_environ(["DOES_NOT_EXIST"])
    assert exported == 0
