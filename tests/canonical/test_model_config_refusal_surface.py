"""Pins the refusal an author reads for an undeclared key under ``models.<role>``.

Sibling of ``test_strict_schema_error_surface.py``, which pins the warn-and-drop
wording for unknown top-level keys. Here the key is refused, so the snapshot holds
each error's location and message as ``RunConfig`` reports them.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import RunConfig

pytestmark = pytest.mark.canonical


def _refusal(agent_extra: dict[str, Any]) -> dict[str, str]:
    with pytest.raises(ValidationError) as refused:
        RunConfig(
            models={"agent": {"provider": "openrouter", "name": "openai/gpt-4o", **agent_extra}},
            orchestrator={},
            evaluation={"output_dir": "x"},
        )
    [error] = refused.value.errors()
    return {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}


def test_undeclared_model_config_key_refusal_shape(canon_snapshot) -> None:
    messages = {
        "model_config": _refusal({"sesion": {"header": "x-session-id"}}),
        "openrouter": _refusal({"openrouter": {"provider_ordr": ["Together"]}}),
        "reasoning": _refusal({"reasoning": {"mdoe": "budget"}}),
    }
    canon_snapshot("model_config_refusal_surface").assert_match(messages, "messages.json")
