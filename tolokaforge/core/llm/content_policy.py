"""Tool-result content format — OpenAI-compatible vs Anthropic native.

Declares the wire shape of tool-result blocks, whether the provider accepts
native image content blocks alongside them, and whether a JSON tool result
that carries a ``$ref`` key is sent as an opaque string (see
:func:`wrap_part_reference_tool_result`). Empty-assistant-content
substitution — a separate wire-shape decision on outgoing assistant turns —
lives on :mod:`tolokaforge.core.llm.message_assembly_policy`.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

__all__ = [
    "ToolContentPolicy",
    "OpenAIContent",
    "AnthropicContent",
    "NovaContent",
    "contains_part_reference",
    "wrap_part_reference_tool_result",
]


@runtime_checkable
class ToolContentPolicy(Protocol):
    """Defines tool result content format for the target model."""

    @property
    def format(self) -> str: ...

    @property
    def supports_images(self) -> bool: ...

    @property
    def wraps_part_reference_tool_results(self) -> bool:
        """Send a JSON tool result that holds a ``{"$ref": "<name>"}`` object as
        ``{"content": "<the original text>"}``.

        A Gemini ``function_response`` reads such an object as a pointer to one
        of its multimodal parts (by ``display_name``) and the Gemini API refuses
        the whole request when no part carries that name. An OpenAI-format
        converter in front of Gemini (a gateway, or litellm itself) parses a
        JSON tool message into ``function_response.response`` verbatim, so a
        JSON Schema fragment in a tool's output — ``"$ref": "#/$defs/X"`` — is
        enough to end the trial at the next model call. Wrapped, the ``$ref``
        sits inside a string and is inert; the model reads the same text.
        """
        ...


class OpenAIContent:
    """OpenAI-compatible text-only tool content.

    Used by the ``default`` preset, ``openai_gpt5``, ``xai_grok``, and
    ``qwen``. The Gemini presets use the ``gemini`` subclass shipped by the
    models wheel, which additionally wraps ``$ref``-carrying results.
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def wraps_part_reference_tool_results(self) -> bool:
        return False


class AnthropicContent:
    """Anthropic native content with image block support."""

    @property
    def format(self) -> str:
        return "anthropic"

    @property
    def supports_images(self) -> bool:
        return True

    @property
    def wraps_part_reference_tool_results(self) -> bool:
        return False


class NovaContent:
    """AWS Bedrock / Nova text-only tool content.

    Wire shape is OpenAI-compatible (no native image blocks on the
    Bedrock OpenAI-passthrough path).
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def wraps_part_reference_tool_results(self) -> bool:
        return False


def contains_part_reference(value: object) -> bool:
    """``True`` when *value* holds a ``{"$ref": "<non-empty string>"}`` object
    at any depth — the shape Gemini resolves against ``function_response.parts``.

    A ``$ref`` whose value is empty or not a string is not a part reference and
    is left alone, as is the text ``$ref`` inside a string value.
    """
    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref:
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def wrap_part_reference_tool_result(text: str) -> str:
    """Return *text* unchanged, or ``{"content": text}`` serialised when *text*
    is a JSON object or array that holds a part reference
    (:func:`contains_part_reference`).

    The original text survives verbatim inside the wrapper, so the model reads
    exactly what the tool returned; only the converter's structural view
    changes. Anything that is not JSON, or JSON without such a ``$ref``, passes
    through untouched. JSON text can spell the key's ``$`` only literally or as
    ``\\u0024``, so the pre-check on those two spellings is exact and keeps every
    other result (non-ASCII ``\\u`` escapes included) away from the parser. A
    result nested deeper than the parser can follow is not JSON it can read
    either (``RecursionError``), and passes through like any other text.
    """
    if "$" not in text and "\\u0024" not in text:
        return text
    if not text.lstrip().startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if not contains_part_reference(parsed):
        return text
    return json.dumps({"content": text}, ensure_ascii=False)
