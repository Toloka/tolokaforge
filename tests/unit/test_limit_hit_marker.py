"""Unit tests for :func:`tolokaforge.core.budgets.write_limit_hit_marker`.

Locks the on-disk contract the CLI's end-banner reader depends on:

- The file lands at ``<output_dir>/LIMIT_HIT.json``.
- The payload matches :class:`LimitHitMarker` (Pydantic ``extra="forbid"``)
  with ISO 8601 UTC ``timestamp`` and a ``which`` value from
  ``{"cost", "time", "sample"}``.
- Overwrite semantics: writing twice with different hits yields the
  second write.
- Unknown ``which`` values are rejected on the Pydantic side.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.budgets import BudgetHit, LimitHitMarker, write_limit_hit_marker

pytestmark = pytest.mark.unit


def test_writes_marker_at_expected_path(tmp_path: Path) -> None:
    hit = BudgetHit(which="cost", threshold=1.0, value_at_hit=1.05, timestamp=1_752_580_496.0)
    marker_path = write_limit_hit_marker(tmp_path, hit)
    assert marker_path == tmp_path / "LIMIT_HIT.json"
    assert marker_path.exists()


def test_marker_payload_matches_pydantic_model(tmp_path: Path) -> None:
    ts = 1_752_580_496.0
    hit = BudgetHit(which="cost", threshold=1.0, value_at_hit=1.05, timestamp=ts)
    marker_path = write_limit_hit_marker(tmp_path, hit)
    payload = json.loads(marker_path.read_text())
    parsed = LimitHitMarker.model_validate(payload)
    assert parsed.which == "cost"
    assert parsed.threshold == pytest.approx(1.0)
    assert parsed.value_at_hit == pytest.approx(1.05)
    expected = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert parsed.timestamp == expected


def test_timestamp_is_iso_8601_utc(tmp_path: Path) -> None:
    hit = BudgetHit(which="time", threshold=60.0, value_at_hit=61.2, timestamp=0.0)
    marker_path = write_limit_hit_marker(tmp_path, hit)
    payload = json.loads(marker_path.read_text())
    # ISO 8601 with a Z suffix (UTC), second-precision.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["timestamp"])
    assert payload["timestamp"] == "1970-01-01T00:00:00Z"


def test_marker_overwrites_existing_file(tmp_path: Path) -> None:
    first = BudgetHit(which="cost", threshold=1.0, value_at_hit=1.05, timestamp=0.0)
    write_limit_hit_marker(tmp_path, first)
    second = BudgetHit(which="sample", threshold=3.0, value_at_hit=3.0, timestamp=0.0)
    marker_path = write_limit_hit_marker(tmp_path, second)
    payload = json.loads(marker_path.read_text())
    assert payload["which"] == "sample"
    assert payload["threshold"] == 3.0


def test_marker_creates_missing_output_dir(tmp_path: Path) -> None:
    """The orchestrator's ``output_dir`` always exists by this point, but
    the writer is defensive — if the directory is missing, it creates it."""
    target = tmp_path / "not-yet-created" / "run_1"
    assert not target.exists()
    hit = BudgetHit(which="cost", threshold=1.0, value_at_hit=1.0, timestamp=0.0)
    marker_path = write_limit_hit_marker(target, hit)
    assert marker_path.exists()
    assert marker_path.parent == target


def test_all_three_which_values_round_trip(tmp_path: Path) -> None:
    for which in ("cost", "time", "sample"):
        marker_dir = tmp_path / which
        hit = BudgetHit(
            which=which,  # type: ignore[arg-type]
            threshold=1.0,
            value_at_hit=1.0,
            timestamp=0.0,
        )
        marker_path = write_limit_hit_marker(marker_dir, hit)
        parsed = LimitHitMarker.model_validate_json(marker_path.read_text())
        assert parsed.which == which


class TestLimitHitMarkerValidation:
    def test_unknown_which_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LimitHitMarker.model_validate(
                {
                    "which": "not-a-real-limit",
                    "threshold": 1.0,
                    "value_at_hit": 1.0,
                    "timestamp": "2026-07-15T00:00:00Z",
                }
            )

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            LimitHitMarker.model_validate(
                {
                    "which": "cost",
                    "threshold": 1.0,
                    "value_at_hit": 1.0,
                    "timestamp": "2026-07-15T00:00:00Z",
                    "unexpected": "field",
                }
            )

    def test_missing_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LimitHitMarker.model_validate({"which": "cost", "threshold": 1.0, "value_at_hit": 1.0})
