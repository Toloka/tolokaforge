"""The shipped pricing table is a copy, and a copy can go out of date.

Refreshing it is a manual command in no CI workflow. A copy 16 days behind
priced ``openai/gpt-5.6-sol`` at $5.00 per million input tokens against an
actual $2.00, overstating a harness sweep 2.5x and inverting its cost ranking —
while every other pricing signal passed, because the arithmetic was right and
only the inputs had aged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tolokaforge.core.pricing import pricing_table_metadata
from tolokaforge.core.pricing_freshness import (
    compare_against_source,
    live_endpoint_prices,
    live_prices,
)

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
        assert meta.updated_at == datetime(2026, 9, 2, 9, 51, 31, tzinfo=UTC)
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


class TestAPinnedRunIsCheckedAgainstTheRouteItCanBeBilledAt:
    """A source publishes one price per model: its cheapest route.

    A config pinning ``openrouter.provider_order`` with fallbacks off cannot be
    billed at that price unless the pinned provider is the cheapest one, so
    checking the table against it refuses correct tables. Observed on a GLM-5.3
    cost study pinned to ``z-ai``: the table matched that provider's published
    rate to the cent and the run was refused against a reseller charging a
    seventeenth of it.
    """

    def test_the_pinned_route_agreeing_is_agreement(self) -> None:
        from tolokaforge.core.pricing import MODEL_PRICING

        model = next(iter(MODEL_PRICING))
        ours = MODEL_PRICING[model]
        cheapest = {rate: value / 17 for rate, value in ours.items()}

        assert compare_against_source([model], {model: cheapest})
        assert not compare_against_source([model], {model: cheapest}, {model: [dict(ours)]})

    def test_agreeing_with_either_permitted_route_is_agreement(self) -> None:
        from tolokaforge.core.pricing import MODEL_PRICING

        model = next(iter(MODEL_PRICING))
        ours = MODEL_PRICING[model]
        other = {rate: value * 3 for rate, value in ours.items()}

        drifts = compare_against_source([model], {model: other}, {model: [dict(other), dict(ours)]})

        assert not drifts

    def test_a_table_no_permitted_route_charges_is_still_refused(self) -> None:
        from tolokaforge.core.pricing import MODEL_PRICING

        model = next(iter(MODEL_PRICING))
        ours = MODEL_PRICING[model]
        route_a = {rate: value * 3 for rate, value in ours.items()}
        route_b = {rate: value * 5 for rate, value in ours.items()}

        drifts = compare_against_source([model], {model: route_a}, {model: [route_a, route_b]})

        assert drifts
        # Reported against the nearest permitted rate, not the furthest.
        nearest = {d.rate: d.theirs for d in drifts}
        for rate, value in route_a.items():
            if rate in nearest:
                assert nearest[rate] == pytest.approx(value)


class TestReadingOneProvidersEndpointPrices:
    @staticmethod
    def _serve(monkeypatch, payload, seen=None):
        class _Response:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _urlopen(url, *a, **k):
            if seen is not None:
                seen.append(url)
            return _Response()

        monkeypatch.setattr("tolokaforge.core.pricing_freshness.urllib_request.urlopen", _urlopen)

    _ENDPOINTS = {
        "data": {
            "endpoints": [
                {"provider_name": "Relace", "pricing": {"prompt": "0.0000000608"}},
                {"provider_name": "Z.AI", "pricing": {"prompt": "0.0000014"}},
            ]
        }
    }

    def test_only_the_pinned_providers_rates_come_back(self, monkeypatch) -> None:
        seen: list[str] = []
        self._serve(monkeypatch, self._ENDPOINTS, seen)

        rates = live_endpoint_prices("https://example.invalid/models", "z-ai/glm-5.3", ["z-ai"])

        assert rates == [{"input": pytest.approx(1.4)}]
        assert seen == ["https://example.invalid/models/z-ai/glm-5.3/endpoints"]

    def test_a_provider_the_source_does_not_list_is_not_checked(self, monkeypatch) -> None:
        self._serve(monkeypatch, self._ENDPOINTS)

        assert (
            live_endpoint_prices("https://example.invalid/models", "z-ai/glm-5.3", ["Nebius"])
            is None
        )

    def test_an_unreachable_source_is_not_checked(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise OSError("down")

        monkeypatch.setattr("tolokaforge.core.pricing_freshness.urllib_request.urlopen", _boom)

        assert live_endpoint_prices("https://example.invalid/models", "m", ["Z.AI"]) is None

    def test_the_provider_key_ignores_case_and_punctuation(self) -> None:
        from tolokaforge.core.pricing_freshness import _provider_key

        assert _provider_key("z-ai") == _provider_key("Z.AI") == "zai"
        assert _provider_key("SiliconFlow") == _provider_key("siliconflow")
        assert _provider_key("Together") != _provider_key("DeepInfra")
