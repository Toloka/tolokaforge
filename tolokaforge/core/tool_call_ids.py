"""The trial's episode-unique tool-call id, and the one rule that derives it.

A provider is free to mint a tool-call id that repeats within one episode.
``moonshotai/kimi-k3`` names each call ``<tool>:<index>``, so calling the same
tool at the same position in two turns emits the same id twice — and the id is
the only key that joins a call to the result it produced.

This module owns the rule that makes the key unique without inventing one: the
k-th occurrence (0-based) of a raw id ``x`` in a trial is keyed ``x`` for k = 0
and ``f"{x}#{k + 1}"`` thereafter. Its properties, in the order they are relied
on:

- **Identity on a repeat-free sequence.** A provider that already mints unique
  ids sees its own ids back, byte for byte.
- **Idempotence.** ``episode_unique_call_ids`` applied to its own output is the
  identity, because that output is repeat-free.
- **Uniqueness of the output**, even when the raw input already contains a
  string of the disambiguated shape: the derived key is re-checked against the
  keys already handed out, so a provider emitting ``x`` twice *and* ``x#2``
  itself still leaves every call with a key of its own.

Two shapes, one rule. :class:`EpisodeUniqueCallIds` is for the caller that sees
one call at a time — the agent loop, which holds one assigner per episode.
:func:`episode_unique_call_ids` is for the caller that holds a whole ordered
view — the grading path, which derives keys per view from that view's own
observation order.

Stdlib only, deliberately: the runtime and the grading path both depend on it,
and neither may reach the other through it.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["EpisodeUniqueCallIds", "episode_unique_call_ids"]

_OCCURRENCE_SEPARATOR = "#"


class EpisodeUniqueCallIds:
    """One episode's assigner: hand it raw ids in order, get unique keys back.

    Trial-scoped. Two episodes never share one, or the second would disambiguate
    against ids the first observed.
    """

    def __init__(self) -> None:
        self._occurrences: dict[str, int] = {}
        self._assigned: set[str] = set()

    def assign(self, raw_id: str) -> str:
        """The episode-unique key for this occurrence of ``raw_id``."""
        occurrence = self._occurrences.get(raw_id, 0)
        while True:
            occurrence += 1
            key = raw_id if occurrence == 1 else f"{raw_id}{_OCCURRENCE_SEPARATOR}{occurrence}"
            if key not in self._assigned:
                break
        self._occurrences[raw_id] = occurrence
        self._assigned.add(key)
        return key


def episode_unique_call_ids(raw_ids: Sequence[str]) -> tuple[str, ...]:
    """``raw_ids`` keyed by occurrence, in the order given — one key per entry."""
    assigner = EpisodeUniqueCallIds()
    return tuple(assigner.assign(raw_id) for raw_id in raw_ids)
