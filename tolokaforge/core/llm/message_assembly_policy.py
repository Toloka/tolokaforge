"""Narrow message-assembly slot — empty-assistant-content filler injection.

Two provider families reject assistant turns whose ``content`` is an empty
string when ``tool_calls`` is present: AWS Bedrock/Nova ("The text field
in the ContentBlock ... is blank") and Moonshot direct (HTTP 400 "the
message at position N with role 'assistant' must not be empty"). Every
other provider accepts the empty shape natively. This slot decides — per
model — whether to substitute a non-empty filler string and, if so, which
string.

Extension contract
------------------
:class:`MessageAssemblyPolicy` is a two-attribute Protocol:

* :attr:`inject_empty_assistant_filler` — when ``True``,
  :meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages` substitutes
  :attr:`empty_assistant_filler` for empty or whitespace-only content on any
  assistant-with-``tool_calls`` turn.
* :attr:`empty_assistant_filler` — the substitution string. Callers must never
  read this attribute when :attr:`inject_empty_assistant_filler` is ``False``;
  the string is unspecified in that case (:class:`NullMessageAssembly` returns
  ``""``).

Two implementations ship:

* :class:`NullMessageAssembly` — no-op; the default for every preset. The
  provider's API accepts empty assistant content alongside ``tool_calls``
  (OpenAI, Anthropic, Gemini-via-OpenRouter, xAI, Qwen, DeepSeek, Cohere,
  MiniMax).
* :class:`FillEmptyAssistantAssembly` — declares the injection ``True`` with
  the filler string configurable per instance. Two presets opt in today:
  ``aws_nova`` / ``aws_nova_openrouter`` (default filler
  ``"I'll help you with that."``) and ``moonshot_kimi_k3`` (filler
  ``" "`` — a bare space is the minimum content that clears Moonshot's
  check without introducing a phrase Kimi could echo back).

The filler string is data on the policy instance, not a hard-coded engine
constant. Universal injection was proven harmful on 2026-04-30 — Gemini
pattern-matched the filler in past assistant turns and echoed
``"I'll help you with that."`` back as its own response content (~26-38 % of
trials on ots_19_airlines). Making the string a per-instance field means a
future provider that needs a different filler declares it at the preset
layer without touching engine code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "MessageAssemblyPolicy",
    "NullMessageAssembly",
    "FillEmptyAssistantAssembly",
]


_DEFAULT_FILLER: str = "I'll help you with that."


@runtime_checkable
class MessageAssemblyPolicy(Protocol):
    """Declares whether empty assistant content (on tool-call turns) is
    substituted with a filler string, and what that string is.

    The two attributes are read together by
    :meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages`; the
    filler is applied only when :attr:`inject_empty_assistant_filler` is
    ``True``.
    """

    @property
    def inject_empty_assistant_filler(self) -> bool: ...

    @property
    def empty_assistant_filler(self) -> str: ...


class NullMessageAssembly:
    """Default policy — empty assistant content stays empty on the wire.

    Used by every preset whose provider API accepts assistant messages with
    empty ``content`` when ``tool_calls`` is present (OpenAI, Anthropic,
    Gemini-via-OpenRouter, xAI, Qwen, DeepSeek, Cohere, MiniMax). Injecting a
    filler on these providers creates a few-shot pattern that some models
    (notably Gemini) echo back as their own content on subsequent turns.
    """

    @property
    def inject_empty_assistant_filler(self) -> bool:
        return False

    @property
    def empty_assistant_filler(self) -> str:
        return ""


class FillEmptyAssistantAssembly:
    """Filler-on policy — substitutes a non-empty string for empty assistant
    content on tool-call turns.

    Two provider families opt in via this class:

    * AWS Bedrock/Nova (``aws_nova`` / ``aws_nova_openrouter`` presets) —
      Bedrock's Converse API rejects assistant messages whose ``content``
      is empty when ``tool_calls`` are present ("The text field in the
      ContentBlock ... is blank"). The default filler
      ``"I'll help you with that."`` clears the check.
    * Moonshot AI direct (``moonshot_kimi_k3`` preset) — Moonshot's
      first-party endpoint rejects the same shape with HTTP 400 "the
      message at position N with role 'assistant' must not be empty".
      Kimi K3 shares family lineage with the echo-back-prone Gemini line,
      so the preset overlays the default with a single space ``" "``: the
      minimum content that clears the check while carrying no imitable
      pattern.

    The filler string is per-instance data (constructor kwarg
    ``empty_assistant_filler``) so a preset overlay can override it via
    ``{name: nova, params: {empty_assistant_filler: "..."}}`` without an
    engine release.
    """

    def __init__(self, *, empty_assistant_filler: str = _DEFAULT_FILLER) -> None:
        self._filler = empty_assistant_filler

    @property
    def inject_empty_assistant_filler(self) -> bool:
        return True

    @property
    def empty_assistant_filler(self) -> str:
        return self._filler
