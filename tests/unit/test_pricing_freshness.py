"""The shipped pricing table is a copy, and a copy can go out of date.

Refreshing it is a manual command in no CI workflow. A copy 16 days behind
priced ``openai/gpt-5.6-sol`` at $5.00 per million input tokens against an
actual $2.00, overstating a harness sweep 2.5x and inverting its cost ranking —
while every other pricing signal passed, because the arithmetic was right and
only the inputs had aged.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tolokaforge.core.pricing import pricing_table_metadata
from tolokaforge.core.pricing_freshness import compare_against_source, live_prices

pytestmark = pytest.mark.unit


def _table(tmp_path, models, meta=None):
    payload = {"models": models}
    if meta is not None:
        payload["_meta"] = meta
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(payload))
    return path


class TestTheTableSaysHowOldItIs:
    def test_the_stamp_and_source_are_read(self, tmp_path) -> None:
        path = _table(
            tmp_path,
            {},
            {
                "source_url": "https://example.invalid/models",
                "updated_at": "2026-09-02T09:51:31+00:00",
            },
        )

        meta = pricing_table_metadata(path)

        assert meta.source_url == "https://example.invalid/models"
        assert meta.updated_at == datetime(2026, 9, 2, 9, 51, 31, tzinfo=timezone.utc)
        assert meta.age is not None and meta.age > timedelta(0)

    @pytest.mark.parametrize(
        "meta",
        [None, {}, {"updated_at": "not-a-date"}, {"updated_at": 17}],
        ids=["absent", "empty", "unparseable", "wrong-type"],
    )
    def test_a_table_that_does_not_say_reports_nothing(self, tmp_path, meta) -> None:
        """A table that does not say when it was fetched is not a table that
        was fetched recently."""
        meta_read = pricing_table_metadata(_table(tmp_path, {}, meta))

        assert meta_read.updated_at is None
        assert meta_read.age is None

    def test_the_shipped_table_carries_its_own_provenance(self) -> None:
        meta = pricing_table_metadata()

        assert meta.source_url
        assert meta.updated_at is not None


class TestDriftAgainstTheSource:
    SOURCE = {
        "openai/gpt-5.6-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.2},
        "anthropic/claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    }

    def test_a_rate_the_source_no_longer_charges_is_drift(self, monkeypatch) -> None:
        """The live defect: billed at $5.00 against an actual $2.00."""
        monkeypatch.setattr(
            "tolokaforge.core.pricing_freshness.MODEL_PRICING",
            {"openai/gpt-5.6-sol": {"input": 5.0, "output": 30.0, "cache_read": 0.5}},
        )

        drifts = compare_against_source(["openrouter/openai/gpt-5.6-sol"], self.SOURCE)

        assert {d.rate for d in drifts} == {"input", "output", "cache_read"}
        assert next(d for d in drifts if d.rate == "input").ratio == pytest.approx(2.5)

    def test_agreement_is_silent(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "tolokaforge.core.pricing_freshness.MODEL_PRICING",
            {"anthropic/claude-sonnet-4.6": {"input": 3.0, "output": 15.0}},
        )

        assert compare_against_source(["openrouter/anthropic/claude-sonnet-4.6"], self.SOURCE) == []

    def test_rounding_is_not_drift(self, monkeypatch) -> None:
        """A per-million conversion rounds; one percent absorbs that without
        absorbing the 43% and 150% drifts this exists to catch."""
        monkeypatch.setattr(
            "tolokaforge.core.pricing_freshness.MODEL_PRICING",
            {"anthropic/claude-sonnet-4.6": {"input": 3.0000001, "output": 15.0}},
        )

        assert compare_against_source(["openrouter/anthropic/claude-sonnet-4.6"], self.SOURCE) == []

    def test_a_rate_only_one_side_carries_is_not_drift(self, monkeypatch) -> None:
        """A rate the table omits is the cache-rate preflight's question, and a
        rate the source omits is not evidence the table is wrong. Reporting
        either here would bury the real thing."""
        monkeypatch.setattr(
            "tolokaforge.core.pricing_freshness.MODEL_PRICING",
            {"anthropic/claude-sonnet-4.6": {"input": 3.0, "output": 15.0, "cache_write": 3.75}},
        )

        assert compare_against_source(["openrouter/anthropic/claude-sonnet-4.6"], self.SOURCE) == []

    def test_a_model_neither_side_prices_is_not_drift(self, monkeypatch) -> None:
        monkeypatch.setattr("tolokaforge.core.pricing_freshness.MODEL_PRICING", {})

        assert compare_against_source(["openrouter/acme/nothing"], self.SOURCE) == []


class TestFailingToAskIsNotAnAnswer:
    """An unreachable source must leave the run alone: refusing because a price
    source blipped would fail runs for a reason unrelated to them."""

    def test_an_unreachable_source_reports_nothing(self) -> None:
        assert live_prices("http://127.0.0.1:9/models") is None

    def test_a_source_that_is_not_json_reports_nothing(self, monkeypatch) -> None:
        class _Response:
            def read(self):
                return b"<html>403</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            "tolokaforge.core.pricing_freshness.urllib_request.urlopen",
            lambda *a, **k: _Response(),
        )

        assert live_prices("https://example.invalid/models") is None
