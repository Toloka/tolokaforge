"""Secret management system for TolokaForge.

This module provides a configurable secret provider chain for resolving secrets
from multiple sources (environment variables, .env files, etc.).

Example:
    >>> from tolokaforge.secrets import SecretManager, SecretConfig
    >>> manager = SecretManager.from_config(SecretConfig.default())
    >>> api_key = manager.get_secret("OPENROUTER_API_KEY")
    >>> env_dict = manager.to_env_dict(["OPENROUTER_API_KEY", "OTHER_KEY"])
"""

from tolokaforge.secrets.config import SecretConfig, SecretSource
from tolokaforge.secrets.manager import (
    MissingSecretError,
    SecretManager,
    get_default,
    init_default,
    init_default_from,
)
from tolokaforge.secrets.providers import (
    DictProvider,
    DotEnvProvider,
    EnvProvider,
    SecretProvider,
)

__all__ = [
    "SecretProvider",
    "EnvProvider",
    "DotEnvProvider",
    "DictProvider",
    "SecretManager",
    "MissingSecretError",
    "SecretConfig",
    "SecretSource",
    "init_default",
    "init_default_from",
    "get_default",
]
