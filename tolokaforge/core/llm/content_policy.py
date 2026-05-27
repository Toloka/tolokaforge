"""Tool-result content format — OpenAI-compatible vs Anthropic native.

Also gates a per-provider wire-shape decision on outgoing assistant
messages: whether empty ``content`` on an assistant-with-tool_calls
turn should be substituted with the literal filler ``"I'll help you
with that."`` before the request reaches the provider.

The filler exists because AWS Bedrock/Nova rejects assistant turns
whose ``content`` is an empty string when ``tool_calls`` are present
(commit 73e01e9e6, 2025-11-25). The substitution was originally
applied unconditionally in
:meth:`tolokaforge.core.llm.client.LLMClient._convert_messages`,
which the 2026-04-30 OTS evaluation revealed to be actively harmful
for Gemini: Gemini 3.x pattern-matches the filler in past assistant
turns and echoes it back as its own content (~26-38% of trials), poisoning
the trajectory.

The default is ``False``: empty content stays empty on the wire.
Nova is the only preset that opts in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "ToolContentPolicy",
    "OpenAIContent",
    "AnthropicContent",
    "NovaContent",
]


@runtime_checkable
class ToolContentPolicy(Protocol):
    """Defines tool result content format for the target model.

    Implementations also declare whether empty assistant content (on
    tool-call turns) must be substituted with a non-empty filler
    string for provider-validation reasons. See module docstring.
    """

    @property
    def format(self) -> str: ...

    @property
    def supports_images(self) -> bool: ...

    @property
    def inject_empty_assistant_filler(self) -> bool: ...


class OpenAIContent:
    """OpenAI-compatible text-only tool content.

    OpenAI's chat-completions API accepts assistant messages with
    empty ``content`` when ``tool_calls`` is present, so no filler is
    needed. The same shape is used for the ``default`` preset, the
    ``gemini`` family, ``openai_gpt5``, ``xai_grok``, and ``qwen``.
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def inject_empty_assistant_filler(self) -> bool:
        return False


class AnthropicContent:
    """Anthropic native content with image block support.

    Anthropic's Messages API accepts empty assistant content when
    ``tool_calls`` is present (the assistant turn is then represented
    by ``tool_use`` content blocks alone). No filler needed.
    """

    @property
    def format(self) -> str:
        return "anthropic"

    @property
    def supports_images(self) -> bool:
        return True

    @property
    def inject_empty_assistant_filler(self) -> bool:
        return False


class NovaContent:
    """AWS Bedrock / Nova text-only tool content.

    Bedrock's converse API rejects assistant messages whose content is
    an empty list / empty string when ``tool_calls`` are present.
    :attr:`inject_empty_assistant_filler` is the only preset that
    keeps the substitution on. Wire shape is otherwise OpenAI-compatible
    (no native image blocks on the OpenAI-style passthrough path used
    by Nova).
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def inject_empty_assistant_filler(self) -> bool:
        return True
