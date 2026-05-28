"""Behavioural guard — Qwen preset wires the dict-map neutralisation
*correctly* (post-bugfix design).

End-to-end (mock) verification that routing ``qwen/qwen3.6-plus`` through
:class:`~tolokaforge.core.llm.client.LLMClient` composes:

1. :class:`~tolokaforge.core.llm.schema_sanitizer.PassthroughSchema` —
   the model sees ``additionalProperties`` natively, matching what its
   training + the task author's docs already taught it.
2. :class:`~tolokaforge.core.llm.prompt_policy.DictMapHints` enriches the
   system prompt with explicit ``CRITICAL:`` formatting guidance that
   names the dict-map parameter (now also detecting ``Optional[Dict[str, T]]``).
3. :class:`~tolokaforge.core.llm.response_policy.JsonCoerceResponse` —
   recovers the stringified-JSON failure mode by ``json.loads``-ing
   container values whose first non-whitespace character is ``[`` or ``{``.

The previous (broken) wiring used ``StrictSchema`` + ``ArrayDictMapResponse``,
which contradicted the hint and the task docs. See the
[`tau_manufacturing_v2` post-fix diagnosis](../../../plans/eval_tau_manufacturing_v2_post_fix_diagnosis.md)
for the empirical case.

No network I/O — only the constructed client's ``capabilities`` are exercised.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm import (
    DictMapHints,
    JsonCoerceResponse,
    LLMClient,
    PassthroughSchema,
)
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture: inline tool schema with a typed dict-map parameter.
#
# We do NOT use ``pydantic.TypeAdapter`` for the dict-map value type here:
# Pydantic emits the nested model as ``$ref: #/$defs/LineItem`` rather than
# inline ``properties``, and the current
# :func:`~tolokaforge.core.llm._dict_maps.detect_dict_maps` helper does not
# resolve ``$ref`` — so the enrichment would silently no-op. Production tool
# schemas (e.g. ``tau_manufacturing_modify_order``) in which Qwen's P2 bug
# was first observed use inline ``properties``; we mirror that shape.
# ---------------------------------------------------------------------------


def _build_tool() -> dict[str, Any]:
    """Tool with a ``Dict[str, LineItem]`` parameter (inline value schema)."""
    return {
        "type": "function",
        "function": {
            "name": "upsert_order",
            "description": "Create or update an order with line items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Order identifier.",
                    },
                    "lines": {
                        "type": "object",
                        "description": "Map of line-id -> line.",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "sku": {
                                    "type": "string",
                                    "description": "Stock keeping unit.",
                                },
                                "qty": {
                                    "type": "integer",
                                    "description": "Quantity ordered.",
                                },
                                "price": {
                                    "type": "number",
                                    "description": "Unit price in USD.",
                                },
                            },
                            "required": ["sku", "qty", "price"],
                        },
                    },
                },
                "required": ["order_id", "lines"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Client fixture — no network I/O; we only inspect ``capabilities``.
# ---------------------------------------------------------------------------


@pytest.fixture
def qwen_client() -> LLMClient:
    """``LLMClient`` configured for ``qwen/qwen3.6-plus`` via OpenRouter."""
    return LLMClient(ModelConfig(provider="openrouter", name="qwen/qwen3.6-plus"))


# ---------------------------------------------------------------------------
# 1. Capabilities wiring — passthrough schema + hints + JSON coercion.
# ---------------------------------------------------------------------------


def test_qwen_client_wires_passthrough_trio(qwen_client: LLMClient) -> None:
    caps = qwen_client.capabilities
    assert isinstance(caps.schema_sanitizer, PassthroughSchema)
    assert isinstance(caps.prompt_policy, DictMapHints)
    assert isinstance(caps.response_policy, JsonCoerceResponse)


# ---------------------------------------------------------------------------
# 2. Prompt enrichment — ``DictMapHints`` injects a CRITICAL block.
# ---------------------------------------------------------------------------


def test_qwen_prompt_policy_enriches_with_dict_map_hint(qwen_client: LLMClient) -> None:
    """The enriched prompt must explicitly mention the ``lines`` param and
    include a concrete JSON example keyed by a line identifier."""
    tools = [_build_tool()]
    base_system = "You are an order-management assistant."

    enriched = qwen_client.capabilities.prompt_policy.enrich(base_system, tools)

    assert enriched is not None
    # 2.1 — DictMapHints marker present.
    assert "CRITICAL:" in enriched, "DictMapHints marker missing from enriched prompt."
    # 2.2 — Dict-map parameter name present.
    param_name_msg = (
        "Dict-map parameter name must appear in the enrichment so the model "
        "is nudged to include it in its tool call."
    )
    assert "lines" in enriched, param_name_msg
    # 2.3 — Example JSON present with our param name and a keyed sub-object.
    assert '"lines"' in enriched
    example_key_msg = (
        "DictMapHints example must use an identifier-shaped key so the "
        "model learns the {key: {...}} nesting pattern."
    )
    assert '"example_key"' in enriched, example_key_msg
    # 2.4 — The base prompt is preserved; enrichment is a suffix.
    suffix_msg = "DictMapHints must append — not replace — the base system prompt."
    assert enriched.startswith(base_system), suffix_msg


# ---------------------------------------------------------------------------
# 3. Sanitizer — Qwen sees the native ``additionalProperties`` shape.
# ---------------------------------------------------------------------------


def test_qwen_sanitizer_preserves_dict_map_shape(qwen_client: LLMClient) -> None:
    """``PassthroughSchema`` leaves the ``additionalProperties: {schema}``
    declaration intact so the model receives the same shape its training +
    the task author's docs both teach (``Dict[str, T]``). The previous
    Stage-2 array conversion was empirically a no-op (Qwen never picked
    the array shape) and contradicted the dict-format hint."""
    tools = [_build_tool()]

    sanitised = qwen_client.capabilities.schema_sanitizer.sanitize(tools)

    params = sanitised[0]["function"]["parameters"]
    lines_schema = params["properties"]["lines"]

    assert (
        lines_schema.get("type") == "object"
    ), f"PassthroughSchema must preserve the dict-map outer type; got {lines_schema!r}"
    assert "additionalProperties" in lines_schema, (
        f"PassthroughSchema must preserve additionalProperties so the "
        f"model sees Dict[str, T] natively; got {lines_schema!r}"
    )


# ---------------------------------------------------------------------------
# 4. Response policy — native dicts pass through unchanged.
# ---------------------------------------------------------------------------


def test_qwen_response_policy_passes_native_dict_through(qwen_client: LLMClient) -> None:
    """When Qwen emits the native dict shape (matching the hint + task docs),
    :class:`JsonCoerceResponse` is a no-op — the tool receives the dict
    unchanged."""
    wire_arguments = {
        "order_id": "PO-A00-5",
        "lines": {
            "SKU-1": {"sku": "SKU-1", "qty": 2, "price": 9.99},
            "SKU-2": {"sku": "SKU-2", "qty": 5, "price": 19.50},
        },
    }

    parsed = qwen_client.capabilities.response_policy.parse_arguments(wire_arguments)

    assert parsed == wire_arguments


# ---------------------------------------------------------------------------
# 5. Response policy decodes stringified JSON dict-map arguments.
# ---------------------------------------------------------------------------


def test_qwen_response_policy_decodes_stringified_dict_map(
    qwen_client: LLMClient,
) -> None:
    """The Qwen failure mode the bugfix targets: model emits the dict
    *as a JSON-encoded string*. ``JsonCoerceResponse`` must decode it
    back to a native dict so Pydantic validation accepts the call."""
    wire_arguments = {
        "order_id": "PO-A00-6",
        "lines": '{"SKU-3": {"sku": "SKU-3", "qty": 1, "price": 4.50}}',
    }

    parsed = qwen_client.capabilities.response_policy.parse_arguments(wire_arguments)

    assert parsed["lines"] == {
        "SKU-3": {"sku": "SKU-3", "qty": 1, "price": 4.50},
    }
    assert parsed["order_id"] == "PO-A00-6"
