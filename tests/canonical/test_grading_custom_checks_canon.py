"""Pin custom_checks routing through the runner-side grading path.

Locks three behaviours that a future refactor of the grade pipeline must
not silently regress:

1. :func:`combine_grade_components` includes the ``custom_checks`` score
   as a weighted contributor when it is present (``>= 0``).
2. A custom-checks-only pack whose executor produced no score falls into
   the empty-active-components guard — returns ``(0.0, False)``, NOT the
   ``(1.0, True)`` silent-pass the pre-Stage-2 runner emitted. This is
   the exact regression this stage exists to close (AGENTS.md Rule 1 —
   surface failures, don't drop them).
3. :func:`_parse_grade_result` decodes the wire ``custom_checks`` list
   into :class:`~tolokaforge.core.models.CustomCheckDetail` on the host
   :class:`~tolokaforge.core.models.Grade`, including the
   ``details_json`` → dict round-trip.
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.trial_grader import _parse_grade_result
from tolokaforge.runner.grading import combine_grade_components

pytestmark = [pytest.mark.canonical, pytest.mark.grading]


class TestCombineGradeComponentsRoutesCustomChecks:
    """The custom_checks score participates in the weighted combine just
    like the other components — added to ``active_components`` when
    ``>= 0`` and weighted per ``combine.weights``.
    """

    def test_custom_checks_score_is_a_weighted_contributor(self) -> None:
        """state_checks=1.0 (w .5) + custom_checks=0.4 (w .5) -> 0.7."""
        components = {
            "hash_score": 1.0,
            "custom_checks_score": 0.4,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 0.5, "custom_checks": 0.5},
            "pass_threshold": 0.75,
            "custom_checks": {"enabled": True, "file": "checks.py"},
            "state_checks": {"jsonpaths": []},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == pytest.approx(0.7)
        assert binary_pass is False

    def test_custom_checks_only_pack_passes_when_score_meets_threshold(self) -> None:
        """A pack with only custom_checks configured passes when the score clears the bar."""
        components = {"custom_checks_score": 1.0}
        grading_config = {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "custom_checks": {"enabled": True, "file": "checks.py"},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == pytest.approx(1.0)
        assert binary_pass is True


class TestCombineGradeComponentsGuardsAgainstSilentPass:
    """Regression lock for the silent-pass that shipped before Stage 2.

    Pre-Stage-2: a custom-checks-only pack whose score came back absent
    (``-1.0`` — the "Not implemented yet" stub) yielded an empty
    ``active_components`` set. Because ``custom_checks`` was not in the
    fail-loud ``actually_configured`` guard, the function returned
    ``(1.0, True)`` — a false success. Stage 2 closes both halves: the
    score is really computed AND the guard recognises custom_checks as a
    configured component.
    """

    def test_custom_checks_only_config_absent_score_fails_via_guard(self) -> None:
        components: dict = {}  # no custom_checks_score present -> not in active
        grading_config = {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 1.0,
            "custom_checks": {"enabled": True, "file": "checks.py"},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 0.0
        assert binary_pass is False


class TestParseGradeResultMapsCustomCheckDetails:
    """The wire ``custom_checks`` list decodes into ``Grade.custom_checks_details``."""

    def test_populates_per_check_details_from_raw_list(self) -> None:
        raw_grade = {
            "binary_pass": True,
            "score": 0.75,
            "components": {"custom_checks": 0.75},
            "reasons": "",
            "custom_checks": [
                {
                    "check_name": "workflow_completed",
                    "status": "passed",
                    "score": 1.0,
                    "message": "workflow finished",
                    "details_json": json.dumps({"steps": 3}),
                },
                {
                    "check_name": "final_state_matches",
                    "status": "failed",
                    "score": 0.5,
                    "message": "counter off by one",
                    "details_json": "",
                },
            ],
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is not None
        assert [d.check_name for d in parsed.custom_checks_details] == [
            "workflow_completed",
            "final_state_matches",
        ]
        assert parsed.custom_checks_details[0].status == "passed"
        assert parsed.custom_checks_details[0].score == 1.0
        assert parsed.custom_checks_details[0].details == {"steps": 3}
        assert parsed.custom_checks_details[1].status == "failed"
        assert parsed.custom_checks_details[1].score == 0.5
        assert parsed.custom_checks_details[1].details is None

    def test_absent_custom_checks_yields_none(self) -> None:
        raw_grade = {
            "binary_pass": True,
            "score": 1.0,
            "components": {},
            "reasons": "",
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is None

    def test_malformed_details_json_drops_details_but_keeps_check(self) -> None:
        """A malformed ``details_json`` string (bad JSON) drops the details payload
        to ``None`` rather than failing the whole grade parse — the audit of *which*
        check produced the malformed payload is preserved on the entry itself.
        """
        raw_grade = {
            "binary_pass": True,
            "score": 1.0,
            "components": {"custom_checks": 1.0},
            "reasons": "",
            "custom_checks": [
                {
                    "check_name": "produced_bad_json",
                    "status": "passed",
                    "score": 1.0,
                    "message": "",
                    "details_json": "{not-json",
                }
            ],
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is not None
        assert len(parsed.custom_checks_details) == 1
        assert parsed.custom_checks_details[0].check_name == "produced_bad_json"
        assert parsed.custom_checks_details[0].details is None
