"""Secret manager — the single source of truth for all secret/API key access.

This module provides the SecretManager class that orchestrates secret retrieval
from an ordered chain of providers, plus module-level singleton helpers.

Example:
    >>> from tolokaforge.secrets import SecretManager, SecretConfig
    >>> manager = SecretManager.from_config(SecretConfig.default())
    >>> api_key = manager.get_secret("API_KEY")
    >>> manager.validate_required(["API_KEY", "DB_URL"])
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tolokaforge.secrets.providers import SecretProvider

if TYPE_CHECKING:
    from tolokaforge.secrets.config import SecretConfig

logger = logging.getLogger(__name__)


class MissingSecretError(Exception):
    """Raised when a required secret is not found in any provider.

    Attributes:
        key: The secret key that was not found.
        providers: List of provider names that were checked.
    """

    def __init__(self, key: str, providers: list[str]) -> None:
        self.key = key
        self.providers = providers
        providers_str = ", ".join(providers) if providers else "none"
        super().__init__(
            f"Required secret '{key}' not found in any provider. Checked providers: {providers_str}"
        )


class SecretManager:
    """Manages secret retrieval from an ordered chain of providers.

    The SecretManager checks providers in order and returns the first match.
    This allows for flexible configuration where, for example, environment
    variables can override .env file values.

    Args:
        providers: Ordered list of secret providers to check.

    Example:
        >>> from tolokaforge.secrets.providers import EnvProvider, DotEnvProvider
        >>> manager = SecretManager([
        ...     DotEnvProvider(".env"),  # Check .env first
        ...     EnvProvider(),           # Fall back to environment
        ... ])
        >>> api_key = manager.get_secret("API_KEY")
    """

    def __init__(self, providers: list[SecretProvider]) -> None:
        """Initialize the SecretManager.

        Args:
            providers: Ordered list of secret providers.
        """
        self._providers = list(providers)
        logger.debug(
            "SecretManager initialized with %d providers: %s",
            len(self._providers),
            [p.name for p in self._providers],
        )

    @property
    def providers(self) -> list[SecretProvider]:
        """Return the list of providers (read-only copy)."""
        return list(self._providers)

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret by checking providers in order.

        Args:
            key: The secret key to look up.

        Returns:
            The secret value from the first provider that has it,
            or None if no provider has the secret.

        Example:
            >>> value = manager.get_secret("API_KEY")
            >>> if value is None:
            ...     print("API_KEY not configured")
        """
        for provider in self._providers:
            value = provider.get_secret(key)
            if value is not None:
                logger.debug(
                    "Secret '%s' found in provider '%s'",
                    key,
                    provider.name,
                )
                return value

        logger.debug(
            "Secret '%s' not found in any of %d providers",
            key,
            len(self._providers),
        )
        return None

    def get_secret_or_raise(self, key: str) -> str:
        """Retrieve a required secret, raising an error if not found.

        Args:
            key: The secret key to look up.

        Returns:
            The secret value.

        Raises:
            MissingSecretError: If the secret is not found in any provider.

        Example:
            >>> try:
            ...     api_key = manager.get_secret_or_raise("API_KEY")
            ... except MissingSecretError as e:
            ...     print(f"Missing: {e.key}")
        """
        value = self.get_secret(key)
        if value is None:
            raise MissingSecretError(key, [p.name for p in self._providers])
        return value

    def has_secret(self, key: str) -> bool:
        """Check if a secret exists in any provider.

        Args:
            key: The secret key to check.

        Returns:
            True if any provider has the secret, False otherwise.

        Example:
            >>> if manager.has_secret("API_KEY"):
            ...     print("API_KEY is configured")
        """
        return any(provider.has_secret(key) for provider in self._providers)

    def validate_required(self, keys: list[str]) -> None:
        """Validate that all required secrets are available.

        Args:
            keys: List of required secret keys.

        Raises:
            MissingSecretError: If any required secret is missing.
                The error contains the first missing key.

        Example:
            >>> manager.validate_required(["API_KEY", "DB_URL", "SECRET_TOKEN"])
        """
        for key in keys:
            if not self.has_secret(key):
                raise MissingSecretError(key, [p.name for p in self._providers])

        logger.debug(
            "Validated %d required secrets: %s",
            len(keys),
            keys,
        )

    def to_env_dict(self, keys: list[str]) -> dict[str, str]:
        """Build a dictionary of secrets for passing to containers.

        Only includes keys that have values (missing keys are skipped).
        This is useful for building the environment dict for container creation.

        Args:
            keys: List of secret keys to include.

        Returns:
            Dictionary mapping keys to their values.
            Keys without values are omitted.

        Example:
            >>> env = manager.to_env_dict(["API_KEY", "DB_URL"])
            >>> container = Container.create(image=img, environment=env)
        """
        env_dict: dict[str, str] = {}
        for key in keys:
            value = self.get_secret(key)
            if value is not None:
                env_dict[key] = value
            else:
                logger.debug(
                    "Secret '%s' not found, omitting from env dict",
                    key,
                )

        logger.debug(
            "Built env dict with %d/%d secrets",
            len(env_dict),
            len(keys),
        )
        return env_dict

    def export_to_environ(self, keys: list[str]) -> int:
        """Export resolved secrets to os.environ for third-party library compatibility.

        Only sets keys that have values. Uses os.environ.setdefault so
        existing env vars are NOT overwritten.

        Args:
            keys: Secret key names to export.

        Returns:
            Number of keys actually exported.
        """
        import os

        exported = 0
        for key in keys:
            value = self.get_secret(key)
            if value is not None:
                os.environ.setdefault(key, value)
                exported += 1
        return exported

    def list_all_keys(self) -> list[str]:
        """List all available secret keys across all providers."""
        keys: set[str] = set()
        for provider in self._providers:
            if hasattr(provider, "list_keys"):
                keys.update(provider.list_keys())
        return sorted(keys)

    def serialize(self, keys: list[str] | None = None) -> dict[str, str]:
        """Serialize resolved secrets for transport to containers.

        Resolves secrets by checking providers in order (same as get_secret).
        Only includes keys that have values.

        Args:
            keys: Explicit list of keys to serialize. If None, serializes
                all available keys from all providers.

        Returns:
            Dict mapping key names to resolved values.
        """
        if keys is None:
            keys = self.list_all_keys()
        return self.to_env_dict(keys)

    @classmethod
    def from_dict(cls, secrets: dict[str, str]) -> SecretManager:
        """Create a SecretManager from a pre-resolved dict.

        Used in containers where secrets are injected as serialized data.
        Future: this can be replaced with a GrPCProvider for on-demand
        secret fetching without changing the SecretManager interface.

        Args:
            secrets: Dict mapping secret key names to values.

        Returns:
            SecretManager backed by a DictProvider.
        """
        from tolokaforge.secrets.providers import DictProvider

        return cls([DictProvider(secrets)])

    @classmethod
    def from_config(cls, config: SecretConfig) -> SecretManager:
        """Create a SecretManager from a configuration object.

        This factory method builds the provider chain based on the
        configuration, respecting the source order.

        Args:
            config: SecretConfig specifying the provider chain.

        Returns:
            Configured SecretManager instance.

        Example:
            >>> config = SecretConfig.default()
            >>> manager = SecretManager.from_config(config)
        """
        from tolokaforge.secrets.config import SecretSource
        from tolokaforge.secrets.providers import DotEnvProvider, EnvProvider

        providers: list[SecretProvider] = []

        for source in config.sources:
            if source == SecretSource.ENV:
                providers.append(EnvProvider())
                logger.debug("Added EnvProvider to chain")
            elif source == SecretSource.DOTENV:
                dotenv_path = config.dotenv_path or ".env"
                providers.append(DotEnvProvider(dotenv_path))
                logger.debug("Added DotEnvProvider(%s) to chain", dotenv_path)

        manager = cls(providers)

        # Validate required keys if specified
        if config.required_keys:
            manager.validate_required(config.required_keys)

        return manager


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------

_default_manager: SecretManager | None = None


def init_default(config: SecretConfig | None = None) -> SecretManager:
    """Initialize the default SecretManager singleton.

    Called once at CLI startup. Subsequent calls return the same instance.
    """
    global _default_manager
    if _default_manager is None:
        if config is None:
            from tolokaforge.secrets.config import SecretConfig as _SC

            config = _SC.default()
        _default_manager = SecretManager.from_config(config)
    return _default_manager


def get_default() -> SecretManager:
    """Get the default SecretManager, auto-initializing with defaults if needed."""
    global _default_manager
    if _default_manager is None:
        from tolokaforge.secrets.config import SecretConfig

        _default_manager = SecretManager.from_config(SecretConfig.default())
    return _default_manager


def init_default_from(manager: SecretManager) -> SecretManager:
    """Set an existing SecretManager as the default singleton.

    Used by the Runner to install a pre-configured SecretManager
    (e.g., from deserialized secrets).

    Args:
        manager: SecretManager instance to use as default.

    Returns:
        The same manager instance.
    """
    global _default_manager
    _default_manager = manager
    return manager
