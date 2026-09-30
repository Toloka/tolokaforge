"""The reasoning probe's classification, which is what its verdicts rest on.

The live calls are not tested here — they cost money and need a key. What is
tested is the judgement the probe applies to what comes back, because a probe
that mislabels an opaque blob as lost reasoning would send someone to fix a
codec that is already correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "scripts" / "analysis"))

from probe_reasoning_transport import (  # noqa: E402
    ProbeResult,
    _where_reasoning_arrives,
)

pytestmark = pytest.mark.unit


class _Message:
    """The shape litellm hands back, with only the fields the probe reads."""

    def __init__(self, reasoning_content=None, provider_specific_fields=None):
        self.reasoning_content = reasoning_content
        self.provider_specific_fields = provider_specific_fields


class TestWhereReasoningArrives:
    def test_a_message_with_no_reasoning_reports_nothing(self) -> None:
        assert _where_reasoning_arrives(_Message()) == ()

    def test_the_canonical_field_is_reported_first(self) -> None:
        msg = _Message(reasoning_content="thinking")

        assert _where_reasoning_arrives(msg) == ("reasoning_content",)

    def test_every_field_carrying_it_is_named(self) -> None:
        """Providers mirror the same text across up to three fields."""
        msg = _Message(
            reasoning_content="thinking",
            provider_specific_fields={
                "reasoning": "thinking",
                "reasoning_details": [{"type": "reasoning.text", "text": "thinking"}],
            },
        )

        assert _where_reasoning_arrives(msg) == (
            "reasoning_content",
            "reasoning",
            "reasoning_details",
        )

    def test_a_summary_block_counts_as_readable(self) -> None:
        msg = _Message(
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.summary", "summary": "s"}]
            }
        )

        assert _where_reasoning_arrives(msg) == ("reasoning_details",)

    def test_an_encrypted_blob_is_not_reasoning_we_could_have_kept(self) -> None:
        """OpenAI and Grok return an opaque blob; nothing is lost by dropping it."""
        msg = _Message(
            provider_specific_fields={
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "rsn_..."}]
            }
        )

        assert _where_reasoning_arrives(msg) == ("encrypted-only",)

    def test_readable_wins_when_a_blob_rides_alongside(self) -> None:
        """Grok emits a summary and an encrypted block together."""
        msg = _Message(
            provider_specific_fields={
                "reasoning_details": [
                    {"type": "reasoning.summary", "summary": "s"},
                    {"type": "reasoning.encrypted", "data": "rsn_..."},
                ]
            }
        )

        assert _where_reasoning_arrives(msg) == ("reasoning_details",)


class TestVerdict:
    """Each verdict is an instruction to a reader, so each must be earned."""

    def test_an_error_overrides_everything(self) -> None:
        assert ProbeResult(slug="m", error="boom").verdict == "ERROR"

    def test_no_reasoning_at_all_is_not_a_defect(self) -> None:
        assert ProbeResult(slug="m").verdict == "no reasoning"

    def test_an_opaque_blob_is_not_a_defect(self) -> None:
        result = ProbeResult(slug="m", arrives_in=("encrypted-only",), extracted=False)

        assert result.verdict == "opaque: nothing to keep"

    def test_readable_reasoning_the_codec_does_not_read_is_a_defect(self) -> None:
        result = ProbeResult(slug="m", arrives_in=("reasoning_content",), extracted=False)

        assert result.verdict == "FIX: arrives readable, not extracted"

    def test_reasoning_we_extract_and_never_send_back_is_a_defect(self) -> None:
        """The defect this whole probe exists for."""
        result = ProbeResult(
            slug="m", arrives_in=("reasoning_content",), extracted=True, replay_emits=False
        )

        assert result.verdict == "FIX: extracted, never replayed"

    def test_a_route_that_refuses_the_replay_is_left_alone(self) -> None:
        result = ProbeResult(
            slug="m",
            arrives_in=("reasoning_content",),
            extracted=True,
            replay_emits=False,
            turn2_reasoning_without_replay=10,
            turn2_reasoning_with_replay=10,
        )

        assert result.verdict == "leave: replay refused upstream"

    def test_a_full_round_trip_is_ok(self) -> None:
        result = ProbeResult(
            slug="m", arrives_in=("reasoning_content",), extracted=True, replay_emits=True
        )

        assert result.verdict == "ok"


class TestHonoured:
    def test_unknown_when_the_second_pair_of_calls_did_not_run(self) -> None:
        assert ProbeResult(slug="m").honoured is None

    def test_more_reasoning_with_the_echo_means_the_route_read_it(self) -> None:
        result = ProbeResult(
            slug="m", turn2_reasoning_without_replay=0, turn2_reasoning_with_replay=19
        )

        assert result.honoured is True

    def test_an_unchanged_answer_means_the_route_ignored_it(self) -> None:
        """Measured on one route: the field arrived and changed nothing."""
        result = ProbeResult(
            slug="m", turn2_reasoning_without_replay=12, turn2_reasoning_with_replay=12
        )

        assert result.honoured is False
