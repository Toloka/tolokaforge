"""The committed trial-contract schemas agree with the models they pin.

``TrialNodeInputs`` / ``TrialResult`` are the JSON shapes an out-of-process
caller serializes to drive one trial. The files under ``docs/schemas/`` are the
versioned contract a caller pins to; this holds them level with the models, so
any change to either model reds here until the schema is regenerated and
``TRIAL_CONTRACT_VERSION`` is deliberately bumped. Regenerate with::

    uv run python scripts/export_trial_contract_schemas.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_contract import (
    CONTRACT_MODELS,
    TRIAL_CONTRACT_VERSION,
    contract_schema_json,
    schema_filename,
)

pytestmark = pytest.mark.canonical

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "schemas"
_REGEN = "uv run python scripts/export_trial_contract_schemas.py"


def test_the_contract_pins_both_payload_models() -> None:
    assert set(CONTRACT_MODELS.values()) == {TrialNodeInputs, TrialResult}


@pytest.mark.parametrize("name", sorted(CONTRACT_MODELS))
def test_committed_schema_matches_the_model(name: str) -> None:
    path = _SCHEMA_DIR / schema_filename(name)
    assert path.exists(), f"missing contract schema {path.name}; run `{_REGEN}`"
    assert path.read_text(encoding="utf-8") == contract_schema_json(CONTRACT_MODELS[name]), (
        f"{path.name} is stale against its model; regenerate with `{_REGEN}` and bump "
        "TRIAL_CONTRACT_VERSION if the shape changed"
    )


def test_current_shape_fields_are_captured() -> None:
    """The committed inputs schema reflects the fields this contract version carries."""
    schema = contract_schema_json(TrialNodeInputs)
    for field in ("job_id", "observability", "model_presets_overlay"):
        assert f'"{field}"' in schema


def test_a_changed_model_would_red_the_ledger() -> None:
    """A deliberate field change must not match the committed schema — the guard trips."""

    class _DriftedResult(TrialResult):
        extra_field: int = 0

    committed = (_SCHEMA_DIR / schema_filename("trial_result")).read_text(encoding="utf-8")
    assert contract_schema_json(_DriftedResult) != committed


def test_the_version_is_reflected_in_the_file_names() -> None:
    assert schema_filename("trial_result") == f"trial_result.v{TRIAL_CONTRACT_VERSION}.json"
    assert issubclass(CONTRACT_MODELS["trial_result"], BaseModel)
