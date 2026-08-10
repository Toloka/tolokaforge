"""Rotation env vars are provider-binding data, not engine hardcodes.

Locks the "new provider needing rotation is a data change" property:
a synthetic provider whose :class:`ProviderBinding` names
``api_keys_env`` / ``api_key_env`` env vars other than OpenRouter's works
without any client-code edit — the loop loads its comma-separated list
from the named env var, and ``_rotate_key`` republishes into the named
primary var on rotation.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tolokaforge.core.llm import client as client_module
from tolokaforge.core.llm.client import AllApiKeysExhaustedError, LLMClient
from tolokaforge.core.llm.providers import ProviderBinding
from tolokaforge.core.models import ModelConfig
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit


InstallBinding = Callable[[ProviderBinding], None]
InstallSecrets = Callable[[dict[str, str]], None]


@pytest.fixture
def isolate_env(tmp_path) -> Iterator[None]:
    """Snapshot ``os.environ`` and point ``OPENROUTER_KEY_FILE`` at a nonexistent path.

    ``_load_api_keys`` reads ``os.environ.get("OPENROUTER_KEY_FILE", "keys.txt")``
    and falls back to reading whatever ``keys.txt`` sits in cwd — a
    non-hermetic path we route around by pointing the env var at a path we
    guarantee does not exist. ``os.environ`` is snapshotted so
    ``_rotate_key``'s republication cannot leak between tests.
    """
    original_env = dict(os.environ)
    os.environ["OPENROUTER_KEY_FILE"] = str(tmp_path / "no-such-file.txt")
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)


@pytest.fixture
def install_secrets() -> Iterator[InstallSecrets]:
    """Install a dict-backed SecretManager for the test's lifetime."""
    original_manager = secrets_manager._default_manager

    def _install(secrets: dict[str, str]) -> None:
        secrets_manager._default_manager = SecretManager([DictProvider(secrets)])

    try:
        yield _install
    finally:
        secrets_manager._default_manager = original_manager


@pytest.fixture
def bind_acme(monkeypatch: pytest.MonkeyPatch) -> InstallBinding:
    """Route ``acme`` to a caller-supplied :class:`ProviderBinding`."""
    original = client_module.get_provider_binding

    def _install(binding: ProviderBinding) -> None:
        def _lookup(provider: str) -> ProviderBinding:
            if provider.lower().split("/", 1)[0] == "acme":
                return binding
            return original(provider)

        monkeypatch.setattr(client_module, "get_provider_binding", _lookup)

    return _install


def _drive_rotation_exhaustion(client: LLMClient) -> AllApiKeysExhaustedError:
    """Force a rotation-triggering error on every attempt and return the exception."""

    def _fake_completion(**_: Any) -> str:
        raise RuntimeError('litellm.AuthenticationError {"code":402} out of credits')

    original = client_module.completion
    client_module.completion = _fake_completion  # type: ignore[assignment]
    try:
        with pytest.raises(AllApiKeysExhaustedError) as excinfo:
            client._call_with_key_rotation({"model": "acme/anything", "messages": []})
    finally:
        client_module.completion = original  # type: ignore[assignment]
    return excinfo.value


def test_rotation_loads_key_list_from_binding_env_var(
    isolate_env: None,
    install_secrets: InstallSecrets,
    bind_acme: InstallBinding,
) -> None:
    """A synthetic provider's ``api_keys_env`` supplies the comma-separated chain."""
    bind_acme(ProviderBinding(api_key_env="ACME_API_KEY", api_keys_env="ACME_API_KEYS"))
    install_secrets({"ACME_API_KEYS": "a1,a2,a3"})

    client = LLMClient(ModelConfig(provider="acme", name="widget-v1"))

    assert client._provider_binding.api_keys_env == "ACME_API_KEYS"
    assert client._provider_binding.api_key_env == "ACME_API_KEY"
    assert client._api_keys == ["a1", "a2", "a3"]
    assert client._current_key_index == 0


def test_rotation_republishes_into_binding_primary_env_var(
    isolate_env: None,
    install_secrets: InstallSecrets,
    bind_acme: InstallBinding,
) -> None:
    """``_rotate_key`` writes each fresh key into ``binding.api_key_env``."""
    bind_acme(ProviderBinding(api_key_env="ACME_API_KEY", api_keys_env="ACME_API_KEYS"))
    install_secrets({"ACME_API_KEYS": "a1,a2,a3"})
    os.environ.pop("ACME_API_KEY", None)
    openrouter_before = os.environ.get("OPENROUTER_API_KEY")

    client = LLMClient(ModelConfig(provider="acme", name="widget-v1"))
    _drive_rotation_exhaustion(client)

    assert client._current_key_index == 2
    assert os.environ.get("ACME_API_KEY") == "a3"
    # The loop is env-var-agnostic: an unrelated provider's env var stays
    # exactly where the test found it.
    assert os.environ.get("OPENROUTER_API_KEY") == openrouter_before


def test_binding_without_rotation_env_var_falls_back_to_single_primary_key(
    isolate_env: None,
    install_secrets: InstallSecrets,
    bind_acme: InstallBinding,
) -> None:
    """``api_keys_env=None`` skips chain loading; ``api_key_env`` still populates one entry."""
    bind_acme(ProviderBinding(api_key_env="ACME_API_KEY", api_keys_env=None))
    install_secrets({"ACME_API_KEY": "solo", "ACME_API_KEYS": "should,be,ignored"})

    client = LLMClient(ModelConfig(provider="acme", name="widget-v1"))

    assert client._api_keys == ["solo"]


def test_binding_without_either_env_var_disables_all_credential_lookup(
    isolate_env: None,
    install_secrets: InstallSecrets,
    bind_acme: InstallBinding,
) -> None:
    """Both fields ``None`` — no key chain, no republication target."""
    bind_acme(ProviderBinding(api_key_env=None, api_keys_env=None))
    install_secrets({"ACME_API_KEYS": "leaked", "ACME_API_KEY": "leaked-too"})

    client = LLMClient(ModelConfig(provider="acme", name="widget-v1"))

    assert client._api_keys == []


def test_non_openrouter_provider_ignores_stray_key_file(
    tmp_path,
    install_secrets: InstallSecrets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray ``keys.txt`` in cwd is a credentials-leak vector for non-OpenRouter bindings.

    ``_load_api_keys`` used to read ``keys.txt`` for every provider whose
    ``api_keys_env`` env var was empty; a Nova client with an OpenRouter
    ``keys.txt`` alongside would then populate ``self._api_keys`` with
    OpenRouter keys, and the first :meth:`_rotate_key` would write one of
    them into ``NOVA_API_KEY`` — cross-provider credential contamination.
    """
    original_env = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keys.txt").write_text("openrouter-key-1\nopenrouter-key-2\n")
    os.environ.pop("OPENROUTER_KEY_FILE", None)
    os.environ.pop("NOVA_API_KEY", None)
    install_secrets({})

    try:
        client = LLMClient(ModelConfig(provider="nova", name="busan-v1"))
        assert client._api_keys == []
        assert not client._rotate_key()
        assert "openrouter-key" not in (os.environ.get("NOVA_API_KEY") or "")
    finally:
        os.environ.clear()
        os.environ.update(original_env)
