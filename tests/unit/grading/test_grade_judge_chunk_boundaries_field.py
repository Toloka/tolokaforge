"""``Grade.judge_chunk_boundaries`` additive field lock.

Locks that (a) the field defaults to ``None`` when a caller omits it,
(b) an explicit partition survives construction verbatim,
(c) ``model_dump(mode="json", exclude_none=True)`` omits the key when
``None`` and includes it verbatim when set, and (d) an old-shape
``grade.yaml`` dict without the field parses cleanly (the additive
default fires — no compat break).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models.grade import Grade

pytestmark = pytest.mark.unit


class TestDefault:
    def test_field_defaults_to_none(self) -> None:
        grade = Grade(binary_pass=True, score=0.5)
        assert grade.judge_chunk_boundaries is None

    def test_explicit_partition_survives_construction(self) -> None:
        grade = Grade(
            binary_pass=True,
            score=0.5,
            judge_chunk_boundaries=[["a", "b"], ["c"]],
        )
        assert grade.judge_chunk_boundaries == [["a", "b"], ["c"]]


class TestModelDump:
    def test_dump_json_exclude_none_omits_field(self) -> None:
        grade = Grade(binary_pass=True, score=0.5)
        dumped = grade.model_dump(mode="json", exclude_none=True)
        assert "judge_chunk_boundaries" not in dumped

    def test_dump_json_exclude_none_includes_field_when_set(self) -> None:
        grade = Grade(
            binary_pass=True,
            score=0.5,
            judge_chunk_boundaries=[["a", "b"], ["c"]],
        )
        dumped = grade.model_dump(mode="json", exclude_none=True)
        assert dumped["judge_chunk_boundaries"] == [["a", "b"], ["c"]]


class TestBackwardCompat:
    def test_legacy_grade_yaml_without_field_parses_cleanly(self) -> None:
        """A grade.yaml dict written before the field existed must parse
        with ``judge_chunk_boundaries=None`` (Grade is not
        ``extra="forbid"`` and the field defaults to ``None``)."""
        legacy_shape = {"binary_pass": True, "score": 0.5}
        grade = Grade.model_validate(legacy_shape)
        assert grade.judge_chunk_boundaries is None
