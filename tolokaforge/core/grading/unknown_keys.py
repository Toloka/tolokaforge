"""What a typed ``grading.yaml`` block does with a key it does not declare.

The refusal an author reads is :func:`tolokaforge.core.unknown_keys.refuse_undeclared_keys`,
shared with every block under ``models.<role>``: it names the offending key, the closest
declared field and the block's whole accepted set. This module adds the file and the block's
address, which the model's own one-line ``extra="forbid"`` refusal does not carry.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.unknown_keys import refuse_undeclared_keys


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
    refuse_undeclared_keys(
        {
            key: value
            for key, value in block.items()
            if not isinstance(key, str) or key not in answered_elsewhere
        },
        model.model_fields,
        owner=model.__name__,
        subject=f"Grading file {grading_path}: the task's own {block_name} block",
        accepted_by=block_name,
        key_noun="grading",
    )
