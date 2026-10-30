"""``docker.services`` refuses a YAML boolean with a message that names the trap."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.models.run_config import RunConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("spelling", ["off", "on", "yes", "false"])
def test_names_the_yaml_boolean_trap(spelling: str) -> None:
    """``on`` / ``off`` read as booleans; the error says so instead of listing modes."""
    docker = yaml.safe_load(f"services:\n  rag_service: {spelling}")
    with pytest.raises(ValidationError, match="YAML reads on/off"):
        RunConfig.model_validate(
            {
                "models": {},
                "orchestrator": {},
                "evaluation": {"task_packs": [], "output_dir": "/tmp/tolokaforge-test-out"},
                "docker": docker,
            }
        )
