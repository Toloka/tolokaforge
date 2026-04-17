"""Secret management system for Docker Foundation Layer.

This module provides a configurable secret provider chain for resolving secrets
from multiple sources (environment variables, .env files, etc.) and passing them
to containers as environment variables.

Example:
    >>> from tolokaforge.docker.secrets import SecretManager, SecretConfig
    >>> manager = SecretManager.from_config(SecretConfig.default())
    >>> api_key = manager.get_secret("OPENROUTER_API_KEY")
    >>> env_dict = manager.to_env_dict(["OPENROUTER_API_KEY", "OTHER_KEY"])
"""

from tolokaforge.docker.secrets.config import SecretConfig
from tolokaforge.docker.secrets.manager import MissingSecretError, SecretManager
from tolokaforge.docker.secrets.providers import (
    DotEnvProvider,
    EnvProvider,
    SecretProvider,
)

__all__ = [
    "SecretProvider",
    "EnvProvider",
    "DotEnvProvider",
    "SecretManager",
    "MissingSecretError",
    "SecretConfig",
]
