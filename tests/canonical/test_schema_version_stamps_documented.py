"""The documented schema stamps agree with the constants that write them.

Every other test that reads a stamp compares it against the constant it came from, so
a bump nobody documented reds nothing: the writer emits the new number, the assertion
reads the new number, and ``docs/OUTPUT_FORMAT.md`` § Schema Version Stamps keeps
telling an analytics consumer the old one. That table is the second source, and a stamp
exists to tell a downstream reader which artifacts and field semantics to expect — so
the table going stale is the whole failure mode the stamp was introduced to prevent.

Two claims: the table's own rows, and the literal ``schema_version: N`` the prose
around them repeats.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.core.models import Trajectory
from tolokaforge.core.output.aggregate_models import AGGREGATE_SCHEMA_VERSION
from tolokaforge.core.output_writer import TRIAL_BUNDLE_SCHEMA_VERSION

pytestmark = pytest.mark.canonical

_DOC = Path(__file__).resolve().parents[2] / "docs" / "OUTPUT_FORMAT.md"
_HEADING = "## Schema Version Stamps"

_SIMULATOR_SCHEMA_VERSION = Trajectory.model_fields["simulator_schema_version"].default

_STAMPS: dict[tuple[str, str], int] = {
    ("trajectory.yaml", "simulator_schema_version"): _SIMULATOR_SCHEMA_VERSION,
    ("metrics.yaml", "schema_version"): TRIAL_BUNDLE_SCHEMA_VERSION,
    ("aggregate.json", "schema_version"): AGGREGATE_SCHEMA_VERSION,
}

# ``simulator_schema_version: 1`` matches the bare pattern too, so the qualified name
# is tried first and the bare one refuses a preceding word character.
_LITERAL_RE = re.compile(r"\b(simulator_schema_version|(?<![\w])schema_version): (\d+)\b")


def _table_rows() -> dict[tuple[str, str], int]:
    """The stamps table's numbered rows, keyed by the file and field they name.

    A row counts as a stamp when its current-value cell is a number, which is the
    property the constants answer for — the struct-typed rows document a shape and
    carry no version. So a value cell edited into something unparseable drops its row
    and fails the key-set assertion rather than passing unread.
    """
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == _HEADING]
    assert len(starts) == 1, f"{_HEADING!r} appears {len(starts)} times in {_DOC.name}"

    rows: dict[tuple[str, str], int] = {}
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.replace("`", "").strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[2].isdigit():
            rows[(cells[0], cells[1])] = int(cells[2])
    return rows


def test_the_stamps_table_documents_every_stamp_at_its_current_value() -> None:
    rows = _table_rows()

    assert set(rows) == set(_STAMPS), (
        f"{_DOC.name} § Schema Version Stamps numbers {sorted(rows)}, the constants "
        f"name {sorted(_STAMPS)} — a row with no constant behind it documents a number "
        "nothing writes, and a constant with no row is an undocumented stamp"
    )
    assert rows == _STAMPS, (
        f"{_DOC.name} § Schema Version Stamps disagrees with the constants that write "
        "the stamps; the constants are authoritative"
    )


def test_the_prose_repeats_no_stamp_value_a_constant_no_longer_writes() -> None:
    """A bumped stamp leaves the sentences around the table naming the old generation.

    The table is the declared source, but the file also spells the current bundle stamp
    into prose and into the provision-failure bundle's description. Membership rather
    than equality per occurrence: which of the two bare ``schema_version`` stamps a
    given sentence is about is not decidable from the line, while a value neither
    constant writes is stale whichever it meant.
    """
    text = _DOC.read_text(encoding="utf-8")
    bare_stamps = {str(TRIAL_BUNDLE_SCHEMA_VERSION), str(AGGREGATE_SCHEMA_VERSION)}
    stale: list[str] = []
    seen = 0

    for name, value in _LITERAL_RE.findall(text):
        seen += 1
        if name == "simulator_schema_version":
            if value != str(_SIMULATOR_SCHEMA_VERSION):
                stale.append(f"{name}: {value} (writes {_SIMULATOR_SCHEMA_VERSION})")
        elif value not in bare_stamps:
            stale.append(f"{name}: {value} (nothing writes it; stamps are {sorted(bare_stamps)})")

    assert seen, f"{_DOC.name} spells no stamp value literally — the sweep passed over nothing"
    assert not stale, f"{_DOC.name} names stamp values no constant writes: {stale}"
