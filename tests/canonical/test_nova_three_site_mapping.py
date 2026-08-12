"""Nova's three sites, computed from the ``providers.yaml`` record.

Locks the *interpretation* of a :class:`ProviderBinding` at the three
places the client applies it: constructor env-set of ``NOVA_API_BASE``,
:meth:`LLMClient._format_model_name` bare-name return, and the per-attempt
kwargs mutation in :meth:`LLMClient._call_with_key_rotation` (endpoint pin,
``api_key`` from ``NOVA_API_KEY``, ``custom_llm_provider`` hint, slug
rewrite). Drives a real :class:`LLMClient` and intercepts litellm's
``completion`` to capture the exact kwargs the transport sees — a client
refactor that breaks the client's application of the Nova binding cannot
stay green by editing a hand-copied paraphrase.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tolokaforge.core.llm import client as client_module
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import ModelConfig
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.canonical


class _CapturedCompletion(RuntimeError):
    """Sentinel raised inside the mocked ``completion`` to abort the call.

    The rotation loop unwraps this as a generic provider error and rewraps it
    into ``RuntimeError("LLM API call failed: ...")``; the test consumes only
    the captured kwargs dict, so response-shape assembly is deliberately
    skipped.
    """


@pytest.fixture
def isolate_env() -> Iterator[None]:
    """Snapshot ``os.environ`` so ``NOVA_API_BASE`` / ``NOVA_API_KEY`` do not leak."""
    original_env = dict(os.environ)
    os.environ.pop("NOVA_API_BASE", None)
    os.environ.pop("NOVA_API_KEY", None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)


@pytest.fixture
def install_nova_secret() -> Iterator[None]:
    """Install a dict-backed SecretManager holding the Nova test key."""
    original_manager = secrets_manager._default_manager
    secrets_manager._default_manager = SecretManager(
        [DictProvider({"NOVA_API_KEY": "nova-test-key"})]
    )
    try:
        yield
    finally:
        secrets_manager._default_manager = original_manager


def test_nova_three_site_mapping_drives_real_client(
    canon_snapshot,
    isolate_env: None,
    install_nova_secret: None,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_completion(**kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        raise _CapturedCompletion("kwargs captured; skip _assemble_result")

    original = client_module.completion
    client_module.completion = _fake_completion  # type: ignore[assignment]
    try:
        client = LLMClient(ModelConfig(provider="nova", name="busan-v1"))
        env_nova_api_base = os.environ.get("NOVA_API_BASE")
        formatted_name = client._format_model_name()
        with pytest.raises(RuntimeError):
            client._call_with_key_rotation({"model": formatted_name, "messages": []})
    finally:
        client_module.completion = original  # type: ignore[assignment]

    call_kwargs = captured["kwargs"]
    payload = {
        "site_1_init_env_set": {
            "env_var": "NOVA_API_BASE",
            "value": env_nova_api_base,
        },
        "site_2_format_model_name": {
            "input_config_name": "busan-v1",
            "output": formatted_name,
        },
        "site_3_call_with_key_rotation_kwargs": {
            "api_base": call_kwargs.get("api_base"),
            "api_key": call_kwargs.get("api_key"),
            "custom_llm_provider": call_kwargs.get("custom_llm_provider"),
            "messages": call_kwargs.get("messages"),
            "model": call_kwargs.get("model"),
        },
    }

    snap = canon_snapshot("nova_three_site_mapping")
    snap.assert_match(payload, "wire.json")
