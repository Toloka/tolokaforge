"""Secret providers for the Docker Foundation Layer.

This module provides abstract and concrete implementations of secret providers
that can retrieve secrets from various sources.

Providers:
    - EnvProvider: Reads secrets from os.environ
    - DotEnvProvider: Parses a .env file (lazy-loaded, does NOT modify os.environ)

Example:
    >>> from tolokaforge.docker.secrets.providers import EnvProvider, DotEnvProvider
    >>> env_provider = EnvProvider()
    >>> dotenv_provider = DotEnvProvider(".env")
    >>> api_key = env_provider.get_secret("API_KEY")
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SecretProvider(ABC):
    """Abstract base class for secret providers.

    Secret providers are responsible for retrieving secrets from a specific source.
    Implementations should be stateless where possible and thread-safe.

    Example:
        >>> class MyProvider(SecretProvider):
        ...     def get_secret(self, key: str) -> str | None:
        ...         return my_secret_store.get(key)
        ...     def has_secret(self, key: str) -> bool:
        ...         return key in my_secret_store
    """

    @abstractmethod
    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret by key.

        Args:
            key: The secret key to look up.

        Returns:
            The secret value if found, None otherwise.
        """

    @abstractmethod
    def has_secret(self, key: str) -> bool:
        """Check if a secret exists.

        Args:
            key: The secret key to check.

        Returns:
            True if the secret exists, False otherwise.
        """

    @property
    def name(self) -> str:
        """Return the provider name for logging/debugging."""
        return self.__class__.__name__


class EnvProvider(SecretProvider):
    """Secret provider that reads from os.environ.

    This provider retrieves secrets directly from the process environment.
    It does not cache values, so changes to os.environ are reflected immediately.

    Example:
        >>> import os
        >>> os.environ["MY_SECRET"] = "secret_value"
        >>> provider = EnvProvider()
        >>> provider.get_secret("MY_SECRET")
        'secret_value'
        >>> provider.has_secret("MY_SECRET")
        True
    """

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret from os.environ.

        Args:
            key: The environment variable name.

        Returns:
            The environment variable value if set, None otherwise.
        """
        value = os.environ.get(key)
        if value is not None:
            logger.debug("EnvProvider: found secret '%s'", key)
        return value

    def has_secret(self, key: str) -> bool:
        """Check if an environment variable exists.

        Args:
            key: The environment variable name.

        Returns:
            True if the environment variable is set, False otherwise.
        """
        return key in os.environ


class DotEnvProvider(SecretProvider):
    """Secret provider that parses a .env file.

    This provider reads and parses a .env file into a dictionary. The file is
    lazy-loaded on first access and cached. It does NOT modify os.environ.

    Supported .env syntax:
        - KEY=value
        - KEY="quoted value"
        - KEY='single quoted value'
        - # comments (lines starting with #)
        - Empty lines are ignored
        - Inline comments after values are NOT supported (for security)
        - Multiline values are NOT supported

    Args:
        path: Path to the .env file. Can be a string or Path object.

    Example:
        >>> provider = DotEnvProvider(".env")
        >>> provider.get_secret("DATABASE_URL")
        'postgres://localhost/db'
    """

    # Regex pattern for parsing .env lines
    # Matches: KEY=value, KEY="value", KEY='value'
    _LINE_PATTERN = re.compile(
        r"""
        ^
        \s*                     # Leading whitespace
        (?:export\s+)?          # Optional 'export ' prefix
        ([A-Za-z_][A-Za-z0-9_]*)  # Key (capture group 1)
        \s*=\s*                 # Equals sign with optional whitespace
        (                       # Value (capture group 2)
            "(?:[^"\\]|\\.)*"   # Double-quoted value (with escape support)
            |
            '(?:[^'\\]|\\.)*'   # Single-quoted value (with escape support)
            |
            [^\s#]*             # Unquoted value (no spaces or comments)
        )
        \s*                     # Trailing whitespace
        $
        """,
        re.VERBOSE,
    )

    def __init__(self, path: str | Path) -> None:
        """Initialize the DotEnvProvider.

        Args:
            path: Path to the .env file.
        """
        self._path = Path(path)
        self._secrets: dict[str, str] | None = None
        self._loaded = False

    @property
    def path(self) -> Path:
        """Return the path to the .env file."""
        return self._path

    def _load(self) -> None:
        """Load and parse the .env file.

        This method is called lazily on first access. If the file doesn't exist,
        an empty dict is used (no error raised).
        """
        if self._loaded:
            return

        self._secrets = {}
        self._loaded = True

        if not self._path.exists():
            logger.debug(
                "DotEnvProvider: .env file not found at '%s', using empty secrets",
                self._path,
            )
            return

        logger.debug("DotEnvProvider: loading .env file from '%s'", self._path)

        try:
            content = self._path.read_text(encoding="utf-8")
            self._secrets = self._parse(content)
            logger.debug(
                "DotEnvProvider: loaded %d secrets from '%s'",
                len(self._secrets),
                self._path,
            )
        except OSError as e:
            logger.warning(
                "DotEnvProvider: failed to read .env file '%s': %s",
                self._path,
                e,
            )
            self._secrets = {}

    def _parse(self, content: str) -> dict[str, str]:
        """Parse .env file content into a dictionary.

        Args:
            content: The raw content of the .env file.

        Returns:
            Dictionary mapping keys to values.
        """
        secrets: dict[str, str] = {}

        for line_num, line in enumerate(content.splitlines(), start=1):
            # Skip empty lines and comments
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            match = self._LINE_PATTERN.match(line)
            if not match:
                logger.debug(
                    "DotEnvProvider: skipping invalid line %d: %r",
                    line_num,
                    line[:50],
                )
                continue

            key = match.group(1)
            value = match.group(2)

            # Remove quotes and handle escapes
            value = self._unquote(value)

            secrets[key] = value

        return secrets

    def _unquote(self, value: str) -> str:
        """Remove quotes from a value and handle escape sequences.

        Args:
            value: The raw value from the .env file.

        Returns:
            The unquoted and unescaped value.
        """
        if not value:
            return value

        # Check for quoted strings
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            # Remove quotes
            value = value[1:-1]
            # Handle common escape sequences
            value = value.replace("\\n", "\n")
            value = value.replace("\\t", "\t")
            value = value.replace("\\r", "\r")
            value = value.replace('\\"', '"')
            value = value.replace("\\'", "'")
            value = value.replace("\\\\", "\\")

        return value

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret from the .env file.

        Args:
            key: The secret key to look up.

        Returns:
            The secret value if found, None otherwise.
        """
        self._load()
        assert self._secrets is not None  # noqa: S101
        value = self._secrets.get(key)
        if value is not None:
            logger.debug("DotEnvProvider: found secret '%s'", key)
        return value

    def has_secret(self, key: str) -> bool:
        """Check if a secret exists in the .env file.

        Args:
            key: The secret key to check.

        Returns:
            True if the secret exists, False otherwise.
        """
        self._load()
        assert self._secrets is not None  # noqa: S101
        return key in self._secrets

    def reload(self) -> None:
        """Force reload the .env file.

        Call this method if the .env file has changed and you want to
        pick up the new values.
        """
        self._loaded = False
        self._secrets = None
        self._load()
