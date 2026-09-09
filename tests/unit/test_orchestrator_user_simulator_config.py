"""Unit tests for ``require_user_simulator_config``.

Locks the fail-loud gate: a run that ships user turns must name the
provider explicitly via ``RunConfig.models["user"]``. There is no
hardcoded default, and the error message names the config site an
operator can fix.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import ModelConfig, require_user_simulator_config

pytestmark = pytest.mark.unit


def test_returns_the_configured_model_when_present() -> None:
    user = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6", temperature=0.9)
    assert require_user_simulator_config(user) is user


def test_raises_with_actionable_message_when_absent() -> None:
    """Absent user model → hard error, not a silent provider default. The
    error message names the config site (models.user) an operator can
    fix and points at project-wide fallbacks via
    project.run_defaults.models.user."""
    with pytest.raises(ValueError) as exc:
        require_user_simulator_config(None)

    message = str(exc.value)
    assert "models.user" in message
    assert "project.run_defaults.models.user" in message
