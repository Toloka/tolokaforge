"""Pytest config for the rubric-calibrator tool tests.

The repo-root ``tests/conftest.py`` auto-skip for ``requires_api`` does NOT reach
``tools/rubric-calibrator/tests/`` (it lives under the repo's ``tests/`` tree), so
the no-key path is replicated here. Combined with ``addopts = ["-m", "not
integration"]`` in ``pyproject.toml``, this makes the live test opt-in:

* a bare ``uv run pytest tests/`` deselects ``-m integration`` entirely;
* even with ``-m integration``, the run is skipped when no API key is present,
  so it never makes an accidental paid call.

Key presence is checked via ``SecretManager`` (no raw ``os.environ`` for keys),
honouring the repo's secrets rule.
"""

from __future__ import annotations

import pytest


def _has_api_key() -> bool:
    """True if any provider key is resolvable via SecretManager."""
    from tolokaforge.secrets import get_default

    secrets = get_default()
    return any(
        secrets.get_secret(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip ``@pytest.mark.requires_api`` tests when no API key is set."""
    if _has_api_key():
        return
    skip_marker = pytest.mark.skip(reason="No LLM API key set (requires_api)")
    for item in items:
        if "requires_api" in item.keywords:
            item.add_marker(skip_marker)
