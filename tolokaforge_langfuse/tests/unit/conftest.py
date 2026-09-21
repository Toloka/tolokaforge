"""Keep plugin credential resolution independent of a developer's .env file."""

import pytest

from tolokaforge.secrets import EnvProvider, SecretManager


@pytest.fixture(autouse=True)
def env_secrets(monkeypatch):
    monkeypatch.setattr(
        "tolokaforge.secrets.manager._default_manager", SecretManager([EnvProvider()])
    )
