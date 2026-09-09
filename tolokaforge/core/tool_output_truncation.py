"""Shared middle-elision helper for engine-loop tool-output truncation.

Reasoning-heavy models are the norm; a first-class engine-loop policy for
bounding tool-output size that lands on the message history is a general
improvement that keeps context growth predictable across every tool the loop
executes. :func:`keep_head_and_tail` is the shared building block that
:class:`~tolokaforge.core.loop.ToolCallingLoop` applies at the ``role=tool``
message-append site when :attr:`~tolokaforge.core.loop.LoopConfig.tool_output_max_chars`
is set.

The helper is intentionally distinct from the per-tool ``_truncate_middle``
helpers in :mod:`tolokaforge.tools.persistent_shell` and
:mod:`tolokaforge.tools.str_replace_editor`: those own tool-authored markers
that carry actionable recovery intent ("re-run a narrower command"), which the
loop layer cannot supply. The loop's marker is deliberately generic.
"""

from __future__ import annotations

__all__ = ["keep_head_and_tail"]


def keep_head_and_tail(text: str, max_chars: int) -> tuple[str, int]:
    """Middle-elide ``text`` down to at most ``max_chars`` chars of head + tail.

    Returns ``(truncated_text, chars_omitted)``. When ``len(text) <= max_chars``
    the input is returned unchanged as ``(text, 0)``. Otherwise the head and
    tail are each ``max_chars // 2`` chars long — so an odd ``max_chars`` drops
    one char from the visible budget — and the marker
    ``f"\\n...[{chars_omitted} chars omitted]...\\n"`` is spliced between them.

    ``max_chars`` must be positive; a non-positive value raises
    :class:`ValueError` so a preset misconfiguration fails loud at
    :func:`~tolokaforge.core.llm.presets.build_capabilities` time.
    """
    if max_chars <= 0:
        raise ValueError(
            f"max_chars must be a positive integer, got {max_chars!r}. "
            "Set tool_output_max_chars to a positive value on the preset, "
            "or leave it unset to disable the cap."
        )
    if len(text) <= max_chars:
        return text, 0
    half = max_chars // 2
    chars_omitted = len(text) - 2 * half
    marker = f"\n...[{chars_omitted} chars omitted]...\n"
    return text[:half] + marker + text[len(text) - half :], chars_omitted
