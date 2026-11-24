"""A task's initial database state read from its state file, not the wire (ADR-0071).

Under ``compute.trial_data_delivery: mount`` the native adapter does not parse the
task's ``initial_state.json_db`` file into ``initial_state.tables``. It records
the file as one of the trial's referenced tool artefacts (ADR-0068) and names it
in ``initial_state.tables_ref``: the artefact path, its SHA-256 and its size.
The runner copies and verifies the file at ``RegisterTrial`` like every other
reference, and reads the trial's tables from that copy only where they are
needed — the database service's initialisation, the registration checks that
read rows, and grading — through :class:`InitialStateFile`, checking the bytes
against the digest at every read.

The tables a file yields are the ones the inline delivery puts on the wire:
:func:`seeded_tables` is the one normalisation both deliveries apply to the
declared JSON (a list is the records, a mapping of mappings is records keyed by
id, anything else is one record), so every grader sees the same tables, the
same values and the same order either way.

Both ends import this module: the native adapter's loader normalises through
:func:`seeded_tables`, and the runner reads through :class:`InitialStateFile`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "InitialStateFile",
    "InitialStateFileError",
    "seeded_records",
    "seeded_table_shapes",
    "seeded_tables",
]

_CHUNK_BYTES = 1024 * 1024


class InitialStateFileError(ValueError):
    """The initial-state file is missing, not the bytes it was described with, or not tables."""


def seeded_records(collection: Any) -> list[dict[str, Any]]:
    """Normalise one seeded collection to the record list its table carries.

    A list is already the records. A mapping is records keyed by id when every
    value is itself a mapping, and one record otherwise — a mapping of
    primitives is the table's single row, not a row per field. Anything else is
    a single record too.
    """
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        values = list(collection.values())
        if values and all(isinstance(value, dict) for value in values):
            return values
    return [collection]


def seeded_tables(json_db: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The tables a declared ``json_db`` mapping seeds, by collection name, in its order."""
    return {name: seeded_records(collection) for name, collection in json_db.items()}


def _collection_shape(collection: Any) -> str:
    """How a seeded collection that is not a list of records is written, in an author's words."""
    if isinstance(collection, dict):
        values = list(collection.values())
        if values and all(isinstance(value, dict) for value in values):
            return "a mapping of records keyed by id"
        return "a single record (a mapping)"
    return f"a {type(collection).__name__}"


def seeded_table_shapes(json_db: Mapping[str, Any]) -> dict[str, str]:
    """The collections of ``json_db`` written as something other than a list, and as what."""
    return {
        name: _collection_shape(collection)
        for name, collection in json_db.items()
        if not isinstance(collection, list)
    }


def _refuse_non_tables(tables: Mapping[str, Any], path: Path) -> None:
    """Refuse tables the wire model would: every table a list of mappings.

    The inline delivery validates ``initial_state.tables`` as
    ``dict[str, list[dict]]`` when the description is built; a file read by
    reference is held to the same shape here, without copying it.
    """
    for name, rows in tables.items():
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise InitialStateFileError(
                f"the initial-state file {path} seeds table {name!r} with records that are "
                "not all mappings; initial_state.tables holds a list of records per table"
            )


def _finite_float_or_null(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class InitialStateFile:
    """The trial's copy of its initial-state file, and what its bytes must hash to.

    ``path`` is the verified copy in the trial's artefact directory; nothing is
    read until a caller asks, and nothing read is kept here.
    """

    path: Path
    sha256: str
    size: int

    def read_json_db(self) -> dict[str, Any]:
        """The file's JSON object, after checking its bytes against :attr:`sha256` and :attr:`size`.

        Raises:
            InitialStateFileError: the file is missing, its bytes do not match, or it
                is not a JSON object.
        """
        # The bytes are decoded and dropped before the parse, so the parse holds the
        # text and the objects it builds, not a third copy of the file.
        text = self._read_verified_text()
        try:
            # NaN, Infinity and a number too large for a float (1e999) read as null, as the
            # engine's serialiser writes them inline.
            parsed = json.loads(
                text, parse_constant=lambda _constant: None, parse_float=_finite_float_or_null
            )
        except ValueError as exc:
            raise InitialStateFileError(
                f"the initial-state file {self.path} is not JSON: {exc}"
            ) from exc
        finally:
            del text
        if not isinstance(parsed, dict):
            raise InitialStateFileError(
                f"the initial-state file {self.path} holds a JSON {type(parsed).__name__}, "
                "not an object of tables"
            )
        return parsed

    def read_tables(self) -> dict[str, list[dict[str, Any]]]:
        """The trial's initial tables, normalised as the inline delivery normalises them."""
        return self.read_tables_and_shapes()[0]

    def read_tables_and_shapes(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """:meth:`read_tables`, and the shape of each collection not written as a list."""
        json_db = self.read_json_db()
        shapes = seeded_table_shapes(json_db)
        tables = seeded_tables(json_db)
        del json_db
        _refuse_non_tables(tables, self.path)
        return tables, shapes

    def iter_chunks(self) -> Iterator[bytes]:
        """The file's bytes in chunks, raising after the last one if they do not match.

        A caller that sends the chunks on learns of a mismatch only once they are
        sent, and nothing it sent takes effect: the raise leaves a chunked request
        body without its terminating chunk, so the database service sees a
        disconnect and creates no trial, and even a body re-framed by a buffering
        proxy is parsed whole as one JSON object before anything is written. The
        iterator is one-shot, so a retry around that request would send nothing.
        """
        digest = hashlib.sha256()
        size = 0
        try:
            handle = self.path.open("rb")
        except OSError as exc:
            raise InitialStateFileError(
                f"the initial-state file {self.path} cannot be read: {exc}"
            ) from exc
        with handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                yield chunk
        self._check(digest.hexdigest(), size)

    async def aiter_chunks(self) -> AsyncIterator[bytes]:
        """:meth:`iter_chunks` for an async HTTP client's request body."""
        for chunk in self.iter_chunks():
            yield chunk

    def _read_verified_text(self) -> str:
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise InitialStateFileError(
                f"the initial-state file {self.path} cannot be read: {exc}"
            ) from exc
        self._check(hashlib.sha256(data).hexdigest(), len(data))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InitialStateFileError(
                f"the initial-state file {self.path} is not UTF-8 JSON: {exc}"
            ) from exc

    def _check(self, sha256: str, size: int) -> None:
        if sha256 == self.sha256 and size == self.size:
            return
        raise InitialStateFileError(
            f"the initial-state file {self.path} has sha256 {sha256} and {size} bytes, "
            f"expected sha256 {self.sha256} and {self.size} bytes: it changed after the "
            "trial was registered"
        )
