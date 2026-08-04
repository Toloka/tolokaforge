"""What a config model does with a key it does not declare, said in one dialect.

Two tiers refuse an author's misspelled key in different words, and both read the
same closest-match suggestion from here: the authoring gate refuses a typed
``grading.yaml`` block outright, naming the file, the layer the key was written in
and the block's whole accepted set, while the Project-layer loader
(:func:`tolokaforge.core.project_loader.construct_config`) warns and points at its
own retirement tracker. A second suggestion dialect would send two authors reading
the same typo to two different sentences.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class GradingKeyLayer(str, Enum):
    """Which layer of the config algebra an author wrote a grading key in.

    A task's effective block is its project's defaults with its own layered on top,
    so the two layers live in different files and an author sent to the wrong one
    fixes nothing.
    """

    TASK = "task"
    PROJECT = "project"

    def address(self, block: str) -> str:
        """Where a key of *block* at this layer was written, in author-facing words."""
        if self is GradingKeyLayer.TASK:
            return f"the task's own {block} block"
        return f"the project defaults beneath it (task_defaults.grading_defaults.{block})"


def suggest_closest_field(model: type[BaseModel], key: str) -> str:
    """The did-you-mean clause for *key* against the fields *model* declares.

    Returned with a leading and a trailing space so a caller composes it into its
    own sentence: the clause carries the suggestion, not the severity.
    """
    suggestion = difflib.get_close_matches(key, list(model.model_fields), n=1)
    if not suggestion:
        return (
            f" — no close match on {model.__name__}. Remove the key or "
            f"check the schema for the correct name. "
        )
    return (
        f" — did you mean '{suggestion[0]}'? "
        f"Rename `{key}` to `{suggestion[0]}` (or remove it if unused). "
    )


def refuse_unknown_grading_keys(
    model: type[BaseModel],
    block: Mapping[str, Any],
    *,
    block_name: str,
    layer: GradingKeyLayer,
    grading_path: Path,
    answered_elsewhere: frozenset[str] = frozenset(),
) -> None:
    """Refuse *block* if it carries a key *model* does not declare.

    The model's own ``extra="forbid"`` is the total guarantee and answers every
    construction path; this is the authoring gate's message for the one path an
    author reads, which the bare ``extra_forbidden`` cannot write: it names the file,
    which of the two layers the key came from, and the whole accepted set, so the fix
    needs no trip to the schema. Every offending key is named in one refusal.

    Args:
        answered_elsewhere: Keys the model answers in its own words rather than as
            unknown ones — a retired key drawing its migration message, which names a
            replacement this refusal knows nothing about. Naming such a key here would
            answer one mistake with two contradicting sentences.

    Raises:
        ValueError: If *block* declares a key outside ``model.model_fields`` that
            *answered_elsewhere* does not hold.
    """
    unknown = [
        key for key in block if key not in model.model_fields and key not in answered_elsewhere
    ]
    if not unknown:
        return
    written = "\n".join(
        f"  - unknown key '{key}'{suggest_closest_field(model, key)}".rstrip() for key in unknown
    )
    accepted = ", ".join(model.model_fields)
    raise ValueError(
        f"Grading file {grading_path}: {block_name} was given a key it does not "
        f"declare, in {layer.address(block_name)}:\n{written}\n"
        f"{block_name} accepts: {accepted}."
    )
