"""Configuration for the secret management system.

This module provides the SecretConfig dataclass for configuring the
secret provider chain.

Example:
    >>> from tolokaforge.secrets.config import SecretConfig, SecretSource
    >>> config = SecretConfig(
    ...     sources=[SecretSource.DOTENV, SecretSource.ENV],
    ...     dotenv_path=".env.local",
    ...     required_keys=["API_KEY"],
    ... )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SecretSource(str, Enum):
    """Enumeration of available secret sources.

    Values:
        ENV: Read from os.environ
        DOTENV: Read from a .env file
    """

    ENV = "env"
    DOTENV = "dotenv"


@dataclass
class SecretConfig:
    """Configuration for the secret management system.

    This dataclass specifies which secret sources to use and in what order.
    The order matters: earlier sources take precedence over later ones.

    Attributes:
        sources: Ordered list of secret sources to check.
        dotenv_path: Path to the .env file (used when DOTENV is in sources).
        required_keys: Optional list of keys that must be present.

    Example:
        >>> config = SecretConfig(
        ...     sources=[SecretSource.DOTENV, SecretSource.ENV],
        ...     dotenv_path=".env",
        ...     required_keys=["OPENROUTER_API_KEY"],
        ... )
    """

    sources: list[SecretSource] = field(default_factory=list)
    dotenv_path: str | Path | None = None
    required_keys: list[str] | None = None

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        # Convert dotenv_path to string if it's a Path
        if isinstance(self.dotenv_path, Path):
            self.dotenv_path = str(self.dotenv_path)

        # Ensure sources is a list
        if not isinstance(self.sources, list):
            self.sources = list(self.sources)

    @classmethod
    def default(cls) -> SecretConfig:
        """Create a default configuration.

        The default configuration checks:
        1. .env file first (for local development overrides)
        2. Environment variables second (for production/CI)

        This order allows developers to override production settings
        locally without modifying environment variables.

        Returns:
            SecretConfig with dotenv → env priority.

        Example:
            >>> config = SecretConfig.default()
            >>> config.sources
            [<SecretSource.DOTENV: 'dotenv'>, <SecretSource.ENV: 'env'>]
        """
        return cls(
            sources=[SecretSource.DOTENV, SecretSource.ENV],
            dotenv_path=".env",
            required_keys=None,
        )

    @classmethod
    def env_only(cls) -> SecretConfig:
        """Create a configuration that only uses environment variables.

        Useful for production environments where secrets are injected
        via environment variables and .env files should be ignored.

        Returns:
            SecretConfig with only ENV source.

        Example:
            >>> config = SecretConfig.env_only()
            >>> config.sources
            [<SecretSource.ENV: 'env'>]
        """
        return cls(
            sources=[SecretSource.ENV],
            dotenv_path=None,
            required_keys=None,
        )

    @classmethod
    def dotenv_only(cls, path: str | Path = ".env") -> SecretConfig:
        """Create a configuration that only uses a .env file.

        Useful for testing or isolated environments where you want
        to ensure secrets come from a specific file.

        Args:
            path: Path to the .env file.

        Returns:
            SecretConfig with only DOTENV source.

        Example:
            >>> config = SecretConfig.dotenv_only(".env.test")
            >>> config.sources
            [<SecretSource.DOTENV: 'dotenv'>]
        """
        return cls(
            sources=[SecretSource.DOTENV],
            dotenv_path=str(path),
            required_keys=None,
        )

    def with_required_keys(self, keys: list[str]) -> SecretConfig:
        """Return a new config with required keys set.

        Args:
            keys: List of required secret keys.

        Returns:
            New SecretConfig with required_keys set.

        Example:
            >>> config = SecretConfig.default().with_required_keys(["API_KEY"])
        """
        return SecretConfig(
            sources=self.sources.copy(),
            dotenv_path=self.dotenv_path,
            required_keys=keys,
        )
