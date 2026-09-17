"""Round-trip lock for the ``chunk_boundaries_json`` string codec.

The codec is the single seam shared by the runner encoder, the grader
encoder, and the host materialiser; drift here would silently misread
every chunked-run replay. This suite locks the four boundary cases:
empty tuple → empty string, non-empty partition → compact JSON, round
trip round-trips, malformed input raises.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.chunk_boundaries_wire import (
    decode_chunk_boundaries,
    encode_chunk_boundaries,
)

pytestmark = pytest.mark.unit


class TestEncode:
    def test_empty_tuple_encodes_to_empty_string(self) -> None:
        assert encode_chunk_boundaries(()) == ""

    def test_non_empty_partition_encodes_compact_json(self) -> None:
        assert encode_chunk_boundaries((("a", "b"), ("c",))) == '[["a","b"],["c"]]'


class TestDecode:
    def test_empty_string_decodes_to_none(self) -> None:
        assert decode_chunk_boundaries("") is None

    def test_json_array_of_arrays_decodes_to_lists(self) -> None:
        assert decode_chunk_boundaries('[["a","b"],["c"]]') == [["a", "b"], ["c"]]

    def test_malformed_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            decode_chunk_boundaries("not json")

    def test_wrong_shape_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="must be a list of lists of strings"):
            decode_chunk_boundaries('["flat"]')
        with pytest.raises(ValueError, match="must be a list of lists of strings"):
            decode_chunk_boundaries("[[1, 2], [3]]")


class TestRoundTrip:
    def test_round_trip_preserves_partition_up_to_list_types(self) -> None:
        original = (("a", "b"), ("c",), ("d", "e", "f"))
        decoded = decode_chunk_boundaries(encode_chunk_boundaries(original))
        assert decoded == [list(chunk) for chunk in original]

    def test_empty_round_trip_stays_empty(self) -> None:
        assert decode_chunk_boundaries(encode_chunk_boundaries(())) is None
