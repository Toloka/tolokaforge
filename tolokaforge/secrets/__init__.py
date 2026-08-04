"""Secret management system for TolokaForge.

This module is the *single source of truth* for every credential read in
the codebase: LLM API keys, database DSNs, OAuth tokens, signing keys —
anything of credential nature. Direct ``os.environ.get`` /
``os.getenv`` /  ``load_dotenv`` /  ``.env`` reads anywhere else are
forbidden; new secret backends ship as new ``SecretProvider`` subclasses.

Typical use:
    >>> from tolokaforge.secrets import get_default
    >>> api_key = get_default().get_secret("OPENROUTER_API_KEY")

CLI entrypoints call ``init_default()`` once at startup. Inside the
runner container, ``init_default_from(...)`` is reconstructed from the
``TOLOKAFORGE_SECRETS_JSON`` environment variable so the same call site
``get_default()`` works on both sides of the host→container boundary.
"""

from tolokaforge.secrets.config import SecretConfig, SecretSource
from tolokaforge.secrets.expand import UnresolvedReferenceError, expand_secret_refs
from tolokaforge.secrets.log_filter import install_global_redactor
from tolokaforge.secrets.manager import (
    MissingSecretError,
    SecretManager,
    get_default,
    get_default_or_none,
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
    "get_default_or_none",
    "install_global_redactor",
    "expand_secret_refs",
    "UnresolvedReferenceError",
]
