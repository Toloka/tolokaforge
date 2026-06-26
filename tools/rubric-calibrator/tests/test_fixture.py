"""Fixture schema + loader tests, incl. the committed golden fixture."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from rubric_calibrator.fixture import GoldenFixture, load_fixture, load_fixtures

pytestmark = pytest.mark.unit

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = FIXTURES_DIR / "refund_partial_credit.yaml"


def test_committed_golden_fixture_loads_and_is_nontrivial():
    fx = load_fixture(GOLDEN)
    # Multi-criterion, mixed binary/graded, with a required gate — non-trivial.
    ids = {c.id for c in fx.rubric.criteria}
    assert ids == {"refund_issued", "amount_quoted", "offered_credit_first", "tone"}
    assert any(c.required for c in fx.rubric.criteria)
    assert any(c.kind == "graded" for c in fx.rubric.criteria)
    # final_db_state present for the in-memory DBReader.
    assert fx.final_db_state and "orders" in fx.final_db_state
    # The human verdict is genuinely mixed (one criterion fails) — not all-pass.
    expected = {e.criterion_id: (e.met, e.score) for e in fx.expected}
    assert expected["offered_credit_first"][0] is False
    assert expected["refund_issued"][0] is True
    assert expected["tone"][1] == pytest.approx(0.9)


def _base_fixture_dict() -> dict:
    return {
        "id": "f1",
        "rubric": {
            "criteria": [
                {"id": "c1", "description": "binary one", "kind": "binary"},
                {"id": "c2", "description": "graded one", "kind": "graded"},
            ]
        },
        "transcript": [{"role": "user", "content": "hi"}],
        "expected": [
            {"criterion_id": "c1", "met": True},
            {"criterion_id": "c2", "score": 0.8},
        ],
    }


def test_valid_fixture_round_trips():
    fx = GoldenFixture(**_base_fixture_dict())
    assert fx.expected_raw("c1") is True
    assert fx.expected_raw("c2") == 0.8


def test_missing_label_fails_loud():
    data = _base_fixture_dict()
    data["expected"] = [{"criterion_id": "c1", "met": True}]
    with pytest.raises(ValueError, match="missing expected labels"):
        GoldenFixture(**data)


def test_extra_label_fails_loud():
    data = _base_fixture_dict()
    data["expected"].append({"criterion_id": "ghost", "met": True})
    with pytest.raises(ValueError, match="unknown criteria"):
        GoldenFixture(**data)


def test_kind_mismatch_binary_with_score_fails():
    data = _base_fixture_dict()
    data["expected"] = [
        {"criterion_id": "c1", "score": 0.9},  # c1 is binary
        {"criterion_id": "c2", "score": 0.8},
    ]
    with pytest.raises(ValueError):
        GoldenFixture(**data)


def test_both_met_and_score_rejected():
    data = _base_fixture_dict()
    data["expected"][0] = {"criterion_id": "c1", "met": True, "score": 1.0}
    with pytest.raises(ValueError, match="exactly one"):
        GoldenFixture(**data)


def test_load_fixtures_expands_directory():
    loaded = load_fixtures([FIXTURES_DIR])
    assert any(fx.id == "refund_partial_credit" for _, fx in loaded)


def test_load_fixtures_rejects_duplicate_ids(tmp_path: Path):
    body = _base_fixture_dict()
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(body))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(body))
    with pytest.raises(ValueError, match="Duplicate fixture ids"):
        load_fixtures([tmp_path])


def test_load_fixtures_missing_path_fails():
    with pytest.raises(FileNotFoundError):
        load_fixtures([Path("/does/not/exist.yaml")])
