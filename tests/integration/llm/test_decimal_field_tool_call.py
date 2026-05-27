"""Stage 8 capability test — :attr:`Capability.DECIMAL_FIELD_TOOL_CALL`.

Guards Stage 1 (P1): every model flagged with this capability must round-
trip a Pydantic-generated ``Decimal`` field through tool-call arguments
without the look-ahead-regex 500. Pre-Stage-1 GPT-5.5 errored on every
Decimal-bearing tool schema because OpenAI's RE2 validator rejected the
``(?!^[-+.]*$)…`` look-ahead that Pydantic emits. Post-fix
:class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema` collapses the
``anyOf`` idiom to ``{"type": "number"}`` and strips every ``pattern`` /
``format`` key — see [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md).

Parametrised over :data:`tests.integration.llm.registry.ALL_MODELS`;
certificates that declare the capability in ``known_unsupported``
(currently the Anthropic family, which uses passthrough schema
sanitisation) auto-skip with an explanatory message.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _Amount(BaseModel):
    amount: Decimal = Field(description="A monetary amount in USD.")
    occurred_at: datetime = Field(description="When the charge was made.")
    note: str | None = None


class _Note(BaseModel):
    text: str = Field(description="Free-form note content.")


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_decimal_field_tool_call(
    cert: ModelCertificate,
    skip_unless_capability_declared,
) -> None:
    """Charge + note tool list must round-trip a Decimal field.

    Assertions:

    1. No exception raised (P1 guard — pre-fix this 500s on GPT-5.5).
    2. ``result.tool_calls`` is non-empty.
    3. Arguments parse as a ``dict``.
    """
    skip_unless_capability_declared(cert, Capability.DECIMAL_FIELD_TOOL_CALL)

    import os

    if not os.getenv(cert.env_key):
        pytest.skip(f"{cert.env_key} not set — skipping live test for {cert.model_id}.")

    tools = [
        _tool("charge", "Charge a card for a given amount.", _Amount),
        _tool("log_note", "Log a short free-form note.", _Note),
    ]
    # Adaptive reasoning mirrors the prior Stage 1 test shape for GPT-5;
    # presets that ignore it (e.g. Claude 4.7) fall through to their own
    # preset-level default.
    client = LLMClient(
        ModelConfig(
            provider=cert.provider,
            name=cert.name,
            reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
        )
    )
    result = client.generate(
        system="You are a charge assistant. Prefer calling tools over replying in prose.",
        messages=[
            Message(
                role=MessageRole.USER,
                content="Charge $42.50 and log a note saying 'test'.",
            )
        ],
        tools=tools,
        tool_choice="auto",
    )
    assert result.tool_calls, f"{cert.model_id}: no tool call returned ({result!r})"
    first_args = result.tool_calls[0].arguments
    if isinstance(first_args, str):
        first_args = json.loads(first_args)
    assert isinstance(first_args, dict), (
        f"{cert.model_id}: tool args must parse as dict, got "
        f"{type(first_args).__name__}: {first_args!r}"
    )
