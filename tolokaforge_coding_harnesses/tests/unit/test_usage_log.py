"""What the middleware proxy's per-request usage records sum to.

The records are written by :mod:`tolokaforge_coding_harnesses.middleware_proxy`
and summed back here, so the fixtures reproduce that writer's key set and its
``None``-for-not-reported convention exactly — a record shape drifting on one
side is meant to show up on the other rather than silently reading as zero.

The summing takes text because the records only exist inside the trial
container; a consumer reads them out of it and hands over the bytes.

Most of what follows is about absence and damage: there is nothing to sum far
more often than there is (no proxy, no provider call, a read of a file that was
never written), and when there is, a proxy killed mid-append leaves a partial
line. Neither may cost a trial its accounting, and neither may turn "nothing
was measured" into "zero was measured".
"""

from __future__ import annotations

import json

import pytest
from tolokaforge_coding_harnesses.usage_log import (
    MIDDLEWARE_PROXY_USAGE_SOURCE,
    sum_harness_usage_records,
    summarise_harness_requests,
)

pytestmark = pytest.mark.unit


def _record(
    *,
    prompt: int | None = None,
    completion: int | None = None,
    cache_read: int | None = None,
    reasoning: int | None = None,
    status: int = 200,
    **extra: object,
) -> str:
    """One line in the shape ``_append_usage_record`` writes."""
    record: dict[str, object] = {
        "timestamp": "2026-09-15T10:00:00+00:00",
        "path": "/v1/chat/completions",
        "status": status,
        "model": "kimi-k2",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": (prompt or 0) + (completion or 0),
        "cache_read_input_tokens": cache_read,
        "reasoning_tokens": reasoning,
    }
    record.update(extra)
    return json.dumps(record)


def _records(*lines: str) -> str:
    """The file's bytes as a read out of the container returns them."""
    return "".join(f"{line}\n" for line in lines)


class TestTheRecordsSumToOnePerTrialTotal:
    def test_every_counter_is_summed_across_requests(self) -> None:
        records = _records(
            _record(prompt=1_000, completion=100, cache_read=600, reasoning=40),
            _record(prompt=2_000, completion=200, cache_read=900, reasoning=60),
        )

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 2
        assert usage.prompt_tokens == 3_000
        assert usage.completion_tokens == 300
        assert usage.cache_read_input_tokens == 1_500
        assert usage.reasoning_tokens == 100
        assert usage.skipped_lines == 0

    def test_a_counter_the_provider_omitted_contributes_nothing(self) -> None:
        """``null`` is "this provider reported no such counter", and adding
        zero for it is arithmetic rather than a claim about what was
        reported."""
        records = _records(_record(prompt=500, completion=10, cache_read=None, reasoning=None))

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.cache_read_input_tokens == 0
        assert usage.reasoning_tokens == 0

    def test_a_refused_request_that_still_reported_usage_counts(self) -> None:
        """A provider that answered 429 and returned a usage block still
        billed for the attempt — and it is the retries the CLI's own summary
        may fold away that make this tap the broader measurement."""
        records = _records(
            _record(prompt=1_000, completion=0, status=429),
            _record(prompt=1_000, completion=50),
        )

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 2
        assert usage.prompt_tokens == 2_000

    def test_a_record_reporting_only_one_counter_still_counts(self) -> None:
        records = _records(_record(prompt=None, completion=17))

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 1
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 17

    def test_an_unknown_key_is_ignored_rather_than_refusing_the_record(self) -> None:
        """The proxy may grow a field before this reader knows it; the counts
        it does recognise are still what the provider billed."""
        records = _records(_record(prompt=100, completion=10, upstream_provider="moonshot"))

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.prompt_tokens == 100

    def test_a_boolean_is_not_a_token_count(self) -> None:
        """``bool`` is an ``int`` subclass, and counting ``True`` as 1 would
        invent a token — the same rule the proxy applies when writing."""
        records = _records(_record(prompt=True, completion=5))  # type: ignore[arg-type]

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 5


class TestDamageCostsOnlyTheDamagedLines:
    def test_a_truncated_line_is_skipped_and_counted(self) -> None:
        records = _records(
            _record(prompt=1_000, completion=10),
            '{"timestamp": "2026-09-15T10:00:01+00:00", "prompt_tok',
            _record(prompt=2_000, completion=20),
        )

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 2
        assert usage.prompt_tokens == 3_000
        assert usage.skipped_lines == 1

    def test_interleaved_stderr_is_skipped(self) -> None:
        """The proxy prints its own tap failures to stderr, which a runtime
        can land in the same stream."""
        records = _records(
            "middleware_proxy: usage tap failed: boom",
            _record(prompt=42, completion=1),
        )

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 1
        assert usage.skipped_lines == 1

    def test_a_blank_line_is_not_damage(self) -> None:
        records = _records(_record(prompt=42, completion=1), "", "")

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.skipped_lines == 0

    def test_a_json_value_that_is_not_an_object_is_skipped(self) -> None:
        records = _records("[1, 2, 3]", _record(prompt=42, completion=1))

        usage = sum_harness_usage_records(records)

        assert usage is not None
        assert usage.requests == 1


class TestNothingMeasuredIsNotZeroMeasured:
    """Every one of these returns ``None``, because a zeroed total would claim
    the trial spent nothing rather than that nobody measured it."""

    def test_nothing_came_back_from_the_read(self) -> None:
        """What a container read of a file the proxy never wrote yields once
        the caller has turned its failure into "no records"."""
        assert sum_harness_usage_records("") is None

    def test_a_file_of_nothing_but_damage(self) -> None:
        assert sum_harness_usage_records(_records("not json", "{oops")) is None

    def test_a_record_reporting_no_counter_at_all(self) -> None:
        """The proxy writes a record only where a usage block was present, so
        a line with no counter is a shape this reader does not recognise."""
        records = _records(json.dumps({"timestamp": "t", "path": "/v1/models", "status": 200}))

        assert sum_harness_usage_records(records) is None


def test_the_tap_has_a_name_a_consumer_can_stamp() -> None:
    assert MIDDLEWARE_PROXY_USAGE_SOURCE == "middleware_proxy"


class TestOnlyCompletionsDecideWhetherTheTrialWasServed:
    """A trial is served when its *completions* are served.

    The proxy taps every path, and several harnesses allowlist a model-list GET
    on their credential gateway. Counting one of those as a served request
    masks a trial whose every completion was refused — the case the summary
    exists to catch.
    """

    @staticmethod
    def _records(*pairs: tuple[str, int]) -> str:
        return "".join(
            json.dumps({"timestamp": "t", "path": path, "status": status, "model": "m"}) + "\n"
            for path, status in pairs
        )

    def test_a_served_model_list_does_not_excuse_refused_completions(self) -> None:
        outcomes = summarise_harness_requests(
            self._records(("/models", 200), ("/chat/completions", 403), ("/chat/completions", 403))
        )

        assert outcomes is not None
        assert outcomes.requests == 2
        assert outcomes.none_succeeded is True

    def test_google_rest_completions_are_recognised(self) -> None:
        outcomes = summarise_harness_requests(
            self._records(("/v1beta/models/gemini-3.6-flash:streamGenerateContent", 403))
        )

        assert outcomes is not None
        assert outcomes.none_succeeded is True

    def test_a_record_without_a_path_still_counts(self) -> None:
        """Older records predate the field; shrinking the evidence on them
        would be a silent behaviour change."""
        outcomes = summarise_harness_requests(
            json.dumps({"timestamp": "t", "status": 403, "model": "m"}) + "\n"
        )

        assert outcomes is not None
        assert outcomes.requests == 1

    def test_one_served_completion_is_enough(self) -> None:
        outcomes = summarise_harness_requests(
            self._records(("/chat/completions", 429), ("/chat/completions", 200))
        )

        assert outcomes is not None
        assert outcomes.none_succeeded is False
