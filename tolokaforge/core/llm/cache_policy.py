"""Explicit prompt-cache-control injection.

:class:`CachePolicy` attaches provider-specific cache markers (Anthropic
``cache_control: {type: ephemeral}``) to the system prompt and tools array
*before* they are handed to :func:`litellm.completion`. The policy receives
``system`` either as the raw ``str`` the caller supplied or as a list of
content-blocks if a previous policy has already transformed it; symmetrically
for ``tools`` (OpenAI-function-style dicts in either case).

Stage 0 shipped only :class:`NoCache`. Stage 6 adds
:class:`AnthropicEphemeralCache` (P8 fix — see
[`plans/llm_reasoning_and_observability_fix.md`](../../../plans/llm_reasoning_and_observability_fix.md)
§ "Stage 6 — Prompt caching" and Part 4.R4 of
[`plans/eval_output_new_diagnosis.md`](../../../plans/eval_output_new_diagnosis.md)).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["AnthropicEphemeralCache", "CachePolicy", "NoCache"]


@runtime_checkable
class CachePolicy(Protocol):
    """Attach ``cache_control`` markers to cacheable prefixes of a request."""

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


class AnthropicEphemeralCache:
    """Attach ``cache_control: {type: ephemeral}`` markers to system + tools.

    Litellm canonical Anthropic prompt-caching shape:

    * ``system`` becomes a list of ``{"type": "text", "text": ..., ...}``
      content-blocks; the **last** block carries ``cache_control``.
    * The **last** entry of ``tools`` carries ``cache_control`` — this caches
      the whole tool-schemas array prefix.
    * ``messages`` is untouched; Stage 6 only caches system + tools.

    The marker is the Anthropic 5-minute-TTL ephemeral default. The apply
    method is idempotent (re-marking already-marked content replaces the
    marker rather than stacking) and operates on shallow copies so caller
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
