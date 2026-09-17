"""JSON-string encoding for ``JudgeResult.chunk_boundaries`` on the runner /
grader ``JudgeReport`` wire.

The encoding mirrors the ``transcript_json`` / ``state_diff_text`` precedents
on the same proto message: a compact JSON string on a proto3 ``string`` field.
Empty string is the wire encoding of "the judge kind did not chunk" (either
``single_shot_rubric`` or any future non-chunking kind); the host materialiser
maps that back to ``None`` on :class:`~tolokaforge.core.models.grade.Grade`.

Shared seam so the runner encoder, grader encoder, and host decoder cannot
drift. No wire-type import here — the module is a plain string codec.
"""

from __future__ import annotations

import json

__all__ = ["decode_chunk_boundaries", "encode_chunk_boundaries"]


def encode_chunk_boundaries(chunks: tuple[tuple[str, ...], ...]) -> str:
    """Encode a per-chunk criterion-id partition into the wire string.

    Returns ``""`` for the empty partition (non-chunking kinds) and a
    compact JSON array-of-arrays otherwise. Chunk order is meaningful,
    so no ``sort_keys`` — the wire preserves original rubric order.
    """
    if not chunks:
        return ""
    return json.dumps([list(chunk) for chunk in chunks], separators=(",", ":"))


def decode_chunk_boundaries(raw: str) -> list[list[str]] | None:
    """Decode a wire string back into a per-chunk criterion-id partition.

    Returns ``None`` on the empty string (the "no chunking" wire encoding),
    a list-of-lists otherwise. A malformed payload raises ``ValueError`` —
    a corrupted wire is a caller bug, never silently swallowed as an empty
    partition (which would misread every chunked-run replay as unchunked).
    """
    if raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chunk_boundaries_json is not valid JSON: {raw!r}") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(chunk, list) and all(isinstance(cid, str) for cid in chunk) for chunk in parsed
    ):
        raise ValueError(
            f"chunk_boundaries_json must be a list of lists of strings; got {parsed!r}"
        )
    return parsed
