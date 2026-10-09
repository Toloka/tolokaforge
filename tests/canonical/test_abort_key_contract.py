"""Adding an excluded termination reason bumps the ``aggregate.json`` generation.

The measurement-fidelity gate's R2 rule demands the full ``infrastructure_aborts``
key set only of bundles stamped at the current ``AGGREGATE_SCHEMA_VERSION``. That
narrowing is what keeps a newly added reason from retroactively condemning every
archived run — but it only works if the stamp actually moves when the key set
does. A reason added without a bump would leave current-generation bundles
checked against a contract they no longer satisfy, and R2 would go quiet on
exactly the live runs it exists to gate.

``ABORT_KEY_CONTRACT_HISTORY`` is the enforcing mechanism rather than a
convention. The newest row must be the live exclusion set at the live version,
and rows must carry strictly increasing versions — so extending
``EXCLUDED_TYPED_REASONS`` cannot pass this file without appending a row, and the
appended row cannot reuse the version the previous set already claimed.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.output.aggregate_models import (
    ABORT_KEY_CONTRACT_HISTORY,
    AGGREGATE_SCHEMA_VERSION,
)

pytestmark = pytest.mark.canonical


def test_the_newest_contract_row_is_the_live_exclusion_set() -> None:
    """The set R2 demands of a current bundle is the set that excludes a trial."""
    _, newest_keys = ABORT_KEY_CONTRACT_HISTORY[-1]

    assert newest_keys == {reason.value for reason in EXCLUDED_TYPED_REASONS}, (
        "EXCLUDED_TYPED_REASONS and the newest ABORT_KEY_CONTRACT_HISTORY row "
        "disagree about which reasons a current bundle must be able to report. "
        "Append a row carrying the new set at a bumped AGGREGATE_SCHEMA_VERSION: "
        "the abort keys are part of the wire generation, so the two move together "
        f"or R2 checks live runs against the wrong contract. Live set: "
        f"{sorted(r.value for r in EXCLUDED_TYPED_REASONS)}, newest row: "
        f"{sorted(newest_keys)}"
    )


def test_the_newest_contract_row_is_the_live_schema_version() -> None:
    """A changed key set that did not bump the stamp is the failure this catches.

    Together with the row-equality lock above, this is what forces the bump: a new
    reason obliges a new row, a new row obliges a greater version, and that version
    has to be the one the writer stamps.
    """
    newest_version, _ = ABORT_KEY_CONTRACT_HISTORY[-1]

    assert newest_version == AGGREGATE_SCHEMA_VERSION, (
        f"the newest abort-key contract is declared at schema version "
        f"{newest_version} but aggregate.json is stamped "
        f"{AGGREGATE_SCHEMA_VERSION}. R2 reads the stamp to decide whether to "
        "demand the keys, so a bundle written now would be checked against a "
        "generation it does not claim to be"
    )


def test_the_history_only_ever_grows() -> None:
    """Append-only, strictly increasing, and never losing a reason.

    The strict increase is the half that makes the bump unavoidable — a new row
    cannot reuse the version the previous key set already claimed. The growth
    check is the other half: a reason removed from the exclusion set is a
    judgement about measurement honesty, not a schema edit, and it would silently
    stop R2 demanding a key that older bundles still carry.
    """
    versions = [version for version, _ in ABORT_KEY_CONTRACT_HISTORY]

    assert versions == sorted(set(versions)), (
        f"ABORT_KEY_CONTRACT_HISTORY versions are not strictly increasing: {versions}. "
        "Two key sets sharing a generation means the stamp no longer identifies "
        "which reasons a file could report"
    )
    for (older_version, older), (newer_version, newer) in zip(
        ABORT_KEY_CONTRACT_HISTORY, ABORT_KEY_CONTRACT_HISTORY[1:], strict=False
    ):
        assert older < newer, (
            f"schema version {newer_version} drops abort keys that {older_version} "
            f"required: {sorted(older - newer)}. Removing a reason from the "
            "exclusion set is a measurement-honesty decision, not a schema bump"
        )
