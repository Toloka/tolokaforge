"""What a typed ``grading.yaml`` block does with a key it does not declare.

The refusal here is the authoring gate's, and it is the message an author reads: it
names the file, the offending key, the closest declared field and the block's whole
accepted set, where the model's own ``extra="forbid"`` refuses in one line carrying
no address. The did-you-mean clause itself is
:func:`tolokaforge.core.deprecations.suggest_closest_field`, shared with the
Project-layer loader's warn-and-drop.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.deprecations import suggest_closest_field


def refuse_unknown_grading_keys(
    model: type[BaseModel],
    block: Mapping[str, Any],
    *,
    block_name: str,
    grading_path: Path,
    answered_elsewhere: frozenset[str] = frozenset(),
) -> None:
    """Refuse *block* if it carries a key *model* does not declare.

    The model's own ``extra="forbid"`` is the total guarantee and answers every
    construction path; this is the authoring gate's message for the one path an
    author reads, which the bare ``extra_forbidden`` cannot write: it names the file
    and the whole accepted set, so the fix needs no trip to the schema. Every
    offending key is named in one refusal.

    Args:
        answered_elsewhere: Keys the model answers in its own words rather than as
            unknown ones — a retired key drawing its migration message, which names a
            replacement this refusal knows nothing about. Naming such a key here would
            answer one mistake with two contradicting sentences.

    Raises:
        ValueError: If *block* declares a key outside ``model.model_fields`` that
            *answered_elsewhere* does not hold, or a key that is not a string.
    """
    unknown = [
        key
        for key in block
        if not isinstance(key, str)
        or (key not in model.model_fields and key not in answered_elsewhere)
    ]
    if not unknown:
        return
    written = "\n".join(f"  - {_refusal_clause(model, key)}" for key in unknown)
    accepted = ", ".join(model.model_fields)
    raise ValueError(
        f"Grading file {grading_path}: the task's own {block_name} block was given a "
        f"key it does not declare:\n{written}\n{block_name} accepts: {accepted}."
    )


def _refusal_clause(model: type[BaseModel], key: Any) -> str:
    """The one line naming *key*, and what to do about it.

    A non-string key gets no suggestion: YAML reads a bare ``on:`` / ``no:`` as a
    boolean and a bare ``3:`` as an int, so the fix is quoting the key rather than
    renaming it — and :func:`suggest_closest_field` would raise a ``TypeError``
    naming neither the file nor the key if handed one.
    """
    if not isinstance(key, str):
        return (
            f"unknown key {key!r}, which YAML read as {type(key).__name__} — grading "
            f"keys must be strings. Quote it to write it as one."
        )
    return f"unknown key '{key}'{suggest_closest_field(model, key)}".rstrip()
