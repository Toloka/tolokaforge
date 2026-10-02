"""Unit tests for the execution-mode seam.

:class:`~tolokaforge.core.execution_mode.ExecutionMode` names how a trial is
driven, and :func:`~tolokaforge.core.execution_mode.select_execution_mode`
classifies it from task metadata. The classifier reads
``agent_harness_command`` but never writes the enum back into metadata; the
conductor branches on its result.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.execution_mode import (
    HARNESS_COMMAND_METADATA_KEY,
    ExecutionMode,
    select_execution_mode,
)

pytestmark = pytest.mark.unit


class TestEnumValues:
    def test_string_values_are_stable(self) -> None:
        # Wire-adjacent: the values are part of the seam's vocabulary.
        assert ExecutionMode.ENGINE_LOOP.value == "engine_loop"
        assert ExecutionMode.DELEGATED.value == "delegated"

    def test_is_a_str_enum(self) -> None:
        assert isinstance(ExecutionMode.DELEGATED, str)

    def test_delegated_sentinel_is_distinct_from_registry_engine_loop(self) -> None:
        # The engine-side enum must not collide with the hyphenated
        # harness-registry sentinel — different concept, different value.
        from tolokaforge_coding_harnesses import ENGINE_LOOP as REGISTRY_ENGINE_LOOP

        assert ExecutionMode.ENGINE_LOOP.value != REGISTRY_ENGINE_LOOP
        assert REGISTRY_ENGINE_LOOP == "engine-loop"


class TestClassifier:
    def test_present_command_selects_delegated(self) -> None:
        metadata = {HARNESS_COMMAND_METADATA_KEY: "claude --print"}
        assert select_execution_mode(metadata) is ExecutionMode.DELEGATED

    def test_absent_command_selects_engine_loop(self) -> None:
        assert select_execution_mode({}) is ExecutionMode.ENGINE_LOOP

    def test_unrelated_metadata_selects_engine_loop(self) -> None:
        assert select_execution_mode({"agent_harness": "engine-loop"}) is (
            ExecutionMode.ENGINE_LOOP
        )

    def test_blank_command_raises(self) -> None:
        with pytest.raises(RuntimeError, match="non-blank string"):
            select_execution_mode({HARNESS_COMMAND_METADATA_KEY: "   "})

    def test_empty_command_raises(self) -> None:
        with pytest.raises(RuntimeError, match="non-blank string"):
            select_execution_mode({HARNESS_COMMAND_METADATA_KEY: ""})

    def test_non_string_command_raises(self) -> None:
        with pytest.raises(RuntimeError, match="non-blank string"):
            select_execution_mode({HARNESS_COMMAND_METADATA_KEY: ["claude", "--print"]})

    def test_the_refusal_names_the_key(self) -> None:
        with pytest.raises(RuntimeError, match=HARNESS_COMMAND_METADATA_KEY):
            select_execution_mode({HARNESS_COMMAND_METADATA_KEY: 0})
