"""Behaviour-locks for the sidecar entrypoint's credential bootstrap.

The sidecar reads its real upstream token from
``TF_GATEWAY_UPSTREAM_TOKEN`` exactly once at process start, hands the
value to a scoped :class:`SecretManager` (via ``init_default_from`` +
``register_runtime_secret``), and installs the global log redactor so
the resolved value is on the scrub set. Nothing else in the sidecar
process reads env for a credential.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.llm_gateway_serve import _bootstrap_secret_manager
from tolokaforge.secrets import get_default

pytestmark = pytest.mark.unit


def test_bootstrap_installs_default_manager_and_returns_the_token(
    monkeypatch: pytest.MonkeyPatch, isolated_secret_manager: None
) -> None:
    monkeypatch.setenv("TF_GATEWAY_UPSTREAM_TOKEN", "sk-real-token-abc")
    resolved = _bootstrap_secret_manager("TF_GATEWAY_UPSTREAM_TOKEN")
    assert resolved == "sk-real-token-abc"
    # Every subsequent read goes through the process default manager, not env.
    assert get_default().get_secret_or_raise("TF_GATEWAY_UPSTREAM_TOKEN") == "sk-real-token-abc"


def test_bootstrap_refuses_empty_token(
    monkeypatch: pytest.MonkeyPatch, isolated_secret_manager: None
) -> None:
    monkeypatch.setenv("TF_GATEWAY_UPSTREAM_TOKEN", "")
    with pytest.raises(SystemExit, match="TF_GATEWAY_UPSTREAM_TOKEN"):
        _bootstrap_secret_manager("TF_GATEWAY_UPSTREAM_TOKEN")


def test_bootstrap_refuses_unset_token(
    monkeypatch: pytest.MonkeyPatch, isolated_secret_manager: None
) -> None:
    monkeypatch.delenv("TF_GATEWAY_UPSTREAM_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TF_GATEWAY_UPSTREAM_TOKEN"):
        _bootstrap_secret_manager("TF_GATEWAY_UPSTREAM_TOKEN")
