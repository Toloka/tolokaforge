"""Regenerate the committed trial-contract JSON schemas under ``docs/schemas/``.

Exports ``TrialNodeInputs`` and ``TrialResult`` JSON schemas to the versioned
files the ledger test pins. Run after a deliberate change to either model (and
after bumping ``TRIAL_CONTRACT_VERSION``)::

    uv run python scripts/export_trial_contract_schemas.py
"""

from __future__ import annotations

from pathlib import Path

from tolokaforge.core.trial_contract import (
    CONTRACT_MODELS,
    contract_schema_json,
    schema_filename,
)

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACT_MODELS.items():
        path = SCHEMA_DIR / schema_filename(name)
        path.write_text(contract_schema_json(model), encoding="utf-8")
        print(f"wrote {path.relative_to(SCHEMA_DIR.parents[1])}")


if __name__ == "__main__":
    main()
