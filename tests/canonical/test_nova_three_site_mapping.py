"""Nova's three sites, computed from the ``providers.yaml`` record.

Locks the *interpretation* of a :class:`ProviderBinding` at the three
places the client applies it: constructor env-set, ``_format_model_name``,
and the per-attempt kwargs mutation in ``_call_with_key_rotation``. The
snapshot is a pure function of the record — no LLMClient construction,
no wire I/O — so a client-implementation refactor cannot silently drift
from what the schema says the transport should look like.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.providers import ProviderBinding, get_provider_binding

pytestmark = pytest.mark.canonical


def _site1_init_env_set(binding: ProviderBinding) -> dict[str, str | None]:
    """Reproduce ``LLMClient.__init__``'s post-lookup env-set for this binding.

    Mirrors the block that runs when neither the gateway nor OpenRouter
    claims the provider: ``os.environ.setdefault(api_base_env, endpoint)``
    when both are populated, no-op otherwise.
    """
    if binding.endpoint and binding.api_base_env:
        return {"env_var": binding.api_base_env, "value": binding.endpoint}
    return {"env_var": None, "value": None}


def _site2_format_model_name(binding: ProviderBinding, config_name: str) -> str:
    """Reproduce ``LLMClient._format_model_name`` for a binding + config name.

    The ``self.config.name.startswith(f"{provider}/")`` short-circuit is
    covered elsewhere; here the input is the bare configured name and the
    only behaviour under test is the ``format_model_name_bare`` branch.
    """
    if binding.format_model_name_bare:
        return config_name
    # Placeholder used only inside this test's snapshot; the real client
    # composes ``f"{config.provider}/{config.name}"`` at the callsite.
    return f"<provider>/{config_name}"


def _site3_call_with_key_rotation_kwargs(
    binding: ProviderBinding,
    starting_model: str,
    resolved_api_key: str,
) -> dict[str, Any]:
    """Reproduce the per-attempt kwargs mutation for a binding.

    ``resolved_api_key`` stands in for what ``SecretManager.get_secret``
    would return; the client's fail-loud on empty is a separate contract.
    """
    kwargs: dict[str, Any] = {"model": starting_model, "messages": []}

    if binding.kwargs_pin_transport:
        kwargs["api_base"] = binding.endpoint
        kwargs["api_key"] = resolved_api_key

    if binding.custom_llm_provider is not None:
        kwargs["custom_llm_provider"] = binding.custom_llm_provider

    if binding.slug_rewrite is not None:
        model = kwargs["model"]
        if binding.slug_rewrite.strip_prefix and model.startswith(
            binding.slug_rewrite.strip_prefix
        ):
            model = model[len(binding.slug_rewrite.strip_prefix) :]
        if binding.slug_rewrite.ensure_prefix and not model.startswith(
            binding.slug_rewrite.ensure_prefix
        ):
            model = binding.slug_rewrite.ensure_prefix + model
        kwargs["model"] = model

    return kwargs


def test_nova_three_site_mapping_from_binding(canon_snapshot) -> None:
    binding = get_provider_binding("nova")
    payload = {
        "site_1_init_env_set": _site1_init_env_set(binding),
        "site_2_format_model_name": {
            "input_config_name": "busan-v1",
            "output": _site2_format_model_name(binding, "busan-v1"),
        },
        "site_3_call_with_key_rotation_kwargs": _site3_call_with_key_rotation_kwargs(
            binding,
            starting_model="nova/busan-v1",
            resolved_api_key="nova-test-key",
        ),
    }

    snap = canon_snapshot("nova_three_site_mapping")
    snap.assert_match(payload, "wire.json")
