"""The words an author reads for a config key the schema does not declare.

One did-you-mean clause and one refusal, so the same typo reads the same way on each
surface that shares them: the Project-layer loader's warn-and-drop
(:func:`tolokaforge.core.project_loader.construct_config`), the grading gate
(:func:`tolokaforge.core.grading.unknown_keys.refuse_unknown_grading_keys`), every block
under ``models.<role>`` (:mod:`tolokaforge.core.models.model_config`) and a preset
overlay's ``openrouter_defaults:`` block (:mod:`tolokaforge.core.llm.presets`).
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Iterable, Mapping
from typing import Any


def suggest_closest_field(fields: Iterable[str], key: str, *, owner: str) -> str:
    """The did-you-mean clause for *key* against the field names a schema declares.

    *owner* names the schema in the no-match sentence. Returned with a leading and a
    trailing space so a caller composes it into its own sentence: the clause carries
    the suggestion, not the severity.
    """
    suggestion = difflib.get_close_matches(key, list(fields), n=1)
    if not suggestion:
        return (
            f" — no close match on {owner}. Remove the key or "
            f"check the schema for the correct name. "
        )
    return (
        f" — did you mean '{suggestion[0]}'? "
        f"Rename `{key}` to `{suggestion[0]}` (or remove it if unused). "
    )


def refuse_undeclared_keys(
    block: Mapping[Any, Any],
    fields: Collection[str],
    *,
    owner: str,
    subject: str | None = None,
    accepted_by: str | None = None,
    key_noun: str = "config",
) -> None:
    """Raise one ``ValueError`` naming every key of *block* outside *fields*.

    Each key gets the :func:`suggest_closest_field` clause against *owner*; a
    non-string key (YAML reads a bare ``on:`` as ``True``) is told to quote itself
    instead, since no rename fixes it. The message opens with *subject* and ends
    with every key *accepted_by* takes; both default to *owner*.
    """
    undeclared = [key for key in block if not isinstance(key, str) or key not in fields]
    if not undeclared:
        return
    clauses = "\n".join(
        f"  - {_undeclared_key_clause(key, fields, owner, key_noun)}" for key in undeclared
    )
    raise ValueError(
        f"{subject or owner} was given a key it does not declare:\n{clauses}\n"
        f"{accepted_by or owner} accepts: {', '.join(fields)}."
    )


def _undeclared_key_clause(key: Any, fields: Collection[str], owner: str, key_noun: str) -> str:
    if not isinstance(key, str):
        return (
            f"unknown key {key!r}, which YAML read as {type(key).__name__} — {key_noun} "
            f"keys must be strings. Quote it to write it as one."
        )
    return f"unknown key '{key}'{suggest_closest_field(fields, key, owner=owner)}".rstrip()
