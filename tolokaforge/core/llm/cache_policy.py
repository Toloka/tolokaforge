"""Explicit prompt-cache-control injection.

:class:`CachePolicy` attaches provider-specific cache markers (Anthropic
``cache_control: {type: ephemeral}``) to the cacheable prefixes of a request
*before* they are handed to :func:`litellm.completion`. The policy runs in
two phases:

* :meth:`CachePolicy.apply` decorates ``system`` + ``tools`` *before*
  :meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages`, so the
  schema sanitizer never sees a ``cache_control`` key it doesn't understand.
  ``system`` may arrive as the raw ``str`` the caller supplied or as a list
  of content-blocks; ``tools`` is an OpenAI-function-style list.
* :meth:`CachePolicy.apply_messages` decorates the wire-shape messages
  *after* ``_convert_messages`` populates them, so marker attachment can
  see the exact list ``litellm.completion`` will receive.

Two implementations ship: :class:`NoCache` (default — pure passthrough on
both hooks) and :class:`AnthropicEphemeralCache` (marks the last block of
the system prompt and the last tools entry with the Anthropic 5-minute-TTL
ephemeral marker).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["AnthropicEphemeralCache", "CachePolicy", "NoCache"]


@runtime_checkable
class CachePolicy(Protocol):
    """Attach ``cache_control`` markers to cacheable prefixes of a request.

    Two hooks compose a single policy:

    * :meth:`apply` runs before wire-message conversion and decorates
      ``system`` + ``tools``.
    * :meth:`apply_messages` runs on the wire-shape messages returned by
      :meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages` and
      may attach markers to individual message blocks. Implementations
      SHOULD return the input unchanged when they have nothing to mark.
    """

    def apply(
        self,
        system: str | list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
    ) -> tuple[
        str | list[dict[str, Any]] | None,
        list[dict[str, Any]] | None,
        list[dict[str, Any]],
    ]:
        """Return ``(system, tools, messages)`` with cache markers attached."""

    def apply_messages(self, wire_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return wire-shape messages with cache-control markers attached.

        Called after ``_convert_messages`` and before ``litellm.completion``
        — receives the same list ``litellm.completion`` will see, and must
        return a shape-compatible list with no semantic changes other than
        marker attachment.
        """


class NoCache:
    """Default: no cache markers attached, request is sent uncached."""

    def apply(
        self,
        system: str | list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
    ) -> tuple[
        str | list[dict[str, Any]] | None,
        list[dict[str, Any]] | None,
        list[dict[str, Any]],
    ]:
        return system, tools, messages

    def apply_messages(self, wire_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return wire_messages


class AnthropicEphemeralCache:
    """Attach ``cache_control: {type: ephemeral}`` markers to Anthropic requests.

    Litellm canonical Anthropic prompt-caching shape:

    * ``system`` becomes a list of ``{"type": "text", "text": ..., ...}``
      content-blocks; the **last** block carries ``cache_control``.
    * The **last** entry of ``tools`` carries ``cache_control`` — this caches
      the whole tool-schemas array prefix.
    * ``apply_messages`` receives the wire-shape messages and returns them
      unchanged.

    The marker is the Anthropic 5-minute-TTL ephemeral default. The apply
    methods are idempotent (re-marking already-marked content replaces the
    marker rather than stacking) and operate on shallow copies so caller
    inputs stay untouched.

    Raises ``TypeError`` if ``system`` is neither ``str`` nor ``list`` nor
    ``None`` — surface failures rather than silently drop caching.
    """

    _MARKER: dict[str, str] = {"type": "ephemeral"}

    def apply(
        self,
        system: str | list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
    ) -> tuple[
        str | list[dict[str, Any]] | None,
        list[dict[str, Any]] | None,
        list[dict[str, Any]],
    ]:
        return self._apply_system(system), self._apply_tools(tools), messages

    def apply_messages(self, wire_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return wire_messages

    def _apply_system(
        self, system: str | list[dict[str, Any]] | None
    ) -> str | list[dict[str, Any]] | None:
        if system is None:
            return None
        if isinstance(system, str):
            if not system:
                # Empty string: refuse to send a cached empty block.
                return system
            return [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": dict(self._MARKER),
                }
            ]
        if isinstance(system, list):
            if not system:
                return system
            out = [dict(block) for block in system]
            last = dict(out[-1])
            last["cache_control"] = dict(self._MARKER)
            out[-1] = last
            return out
        raise TypeError(
            f"AnthropicEphemeralCache.apply: system must be str | list | None, "
            f"got {type(system).__name__}"
        )

    def _apply_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return tools
        out = [dict(t) for t in tools]
        last = dict(out[-1])
        last["cache_control"] = dict(self._MARKER)
        out[-1] = last
        return out
