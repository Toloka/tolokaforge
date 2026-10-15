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
that name a tool-specific recovery ("re-run a narrower command"), which the
loop layer cannot supply. The loop's marker states only what holds for every
tool: how much of how much was dropped, and that a narrower request returns
less — the model learns the output was cut and how to get the part it lacks
without knowing which tool produced it.
"""

from __future__ import annotations

__all__ = ["keep_head_and_tail", "validated_output_max_chars"]


def validated_output_max_chars(value: object, path: str, *, context: str = "") -> int | None:
    """*value* as a tool-output cap declared at *path*, ``None`` when it declares none.

    One rule for every layer that names a cap — a preset's or run config's
    ``tool_output_max_chars`` and a task's ``tools.<actor>[.<name>].output_max_chars``:
    a positive int, or ``None`` for no cap. Validated rather than coerced, because
    each is authoring surface: ``"200000"`` or ``1.5`` would otherwise become a cap
    nobody wrote, ``True`` would pass as ``1``, and ``0`` would surface only at the
    first tool result of the first trial (:func:`keep_head_and_tail` refuses it).

    Raises:
        ValueError: *value* is neither ``None`` nor a positive int. The message
            names *path* and, when given, *context* (the preset or task it is in).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        where = f" in {context}" if context else ""
        raise ValueError(
            f"{path} must be a positive integer or null "
            f"(got {type(value).__name__}={value!r}){where}"
        )
    return value


def keep_head_and_tail(text: str, max_chars: int) -> tuple[str, int]:
    """Middle-elide ``text`` down to at most ``max_chars`` chars of head + tail.

    Returns ``(truncated_text, chars_omitted)``. When ``len(text) <= max_chars``
    the input is returned unchanged as ``(text, 0)``. Otherwise the head and
    tail are each ``max_chars // 2`` chars long — so an odd ``max_chars`` drops
    one char from the visible budget — and the marker
    ``f"\\n...[{chars_omitted} of {len(text)} chars omitted; narrow the request
    to see the rest]...\\n"`` is spliced between them.

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
    marker = (
        f"\n...[{chars_omitted} of {len(text)} chars omitted; "
        "narrow the request to see the rest]...\n"
    )
    return text[:half] + marker + text[len(text) - half :], chars_omitted
