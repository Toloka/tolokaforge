"""The reasoning probe's verdicts, which are what a reader acts on.

The live calls are not tested here — they cost money and need a key. What is
tested is the judgement the probe applies to what comes back, because a probe
that mislabels an opaque blob as lost reasoning would send someone to fix a
codec that is already correct.

Where reasoning arrives is no longer the probe's own question: it reads through
``tolokaforge.core.llm.reasoning_transport``, so the enumeration is tested in
:mod:`tests.unit.llm.test_reasoning_transport` and the two are pinned together
by :mod:`tests.canonical.test_reasoning_transport_probe_agreement`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "scripts" / "analysis"))

from probe_reasoning_transport import ProbeResult  # noqa: E402

from tolokaforge.core.llm.reasoning_transport import ArrivingReasoning

pytestmark = pytest.mark.unit


class TestVerdict:
    """Each verdict is an instruction to a reader, so each must be earned."""

    def test_an_error_overrides_everything(self) -> None:
        assert ProbeResult(slug="m", error="boom").verdict == "ERROR"

    def test_no_reasoning_at_all_is_not_a_defect(self) -> None:
        assert ProbeResult(slug="m").verdict == "no reasoning"

    def test_an_opaque_blob_is_not_a_defect(self) -> None:
        result = ProbeResult(slug="m", arrived=ArrivingReasoning(opaque=True), extracted=False)

        assert result.verdict == "opaque: nothing to keep"

    def test_readable_reasoning_the_codec_does_not_read_is_a_defect(self) -> None:
        result = ProbeResult(
            slug="m", arrived=ArrivingReasoning(readable=("reasoning_content",)), extracted=False
        )

        assert result.verdict == "FIX: arrives readable, not extracted"

    def test_reasoning_we_extract_and_never_send_back_is_a_defect(self) -> None:
        """The defect this whole probe exists for."""
        result = ProbeResult(
            slug="m",
            arrived=ArrivingReasoning(readable=("reasoning_content",)),
            extracted=True,
            replay_emits=False,
        )

        assert result.verdict == "FIX: extracted, never replayed"

    def test_a_route_that_refuses_the_replay_is_left_alone(self) -> None:
        result = ProbeResult(
            slug="m",
            arrived=ArrivingReasoning(readable=("reasoning_content",)),
            extracted=True,
            replay_emits=False,
            turn2_reasoning_without_replay=10,
            turn2_reasoning_with_replay=10,
        )

        assert result.verdict == "leave: replay refused upstream"

    def test_a_full_round_trip_is_ok(self) -> None:
        result = ProbeResult(
            slug="m",
            arrived=ArrivingReasoning(readable=("reasoning_content",)),
            extracted=True,
            replay_emits=True,
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
