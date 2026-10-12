"""Contract: :attr:`AuthoringReport.hints` is never fatal under any ``fail_on``.

The rubric-anchor lint reports a ``kind: graded`` criterion with no
``expected:`` field as a hint, and a hint must not fail a pack no matter how
strictly the caller has set :class:`GradingFindingSeverity`. This is the
load-bearing invariant separating the new channel from :attr:`advisories`,
which are already fatal by default: swapping the channel would fail every
existing pack under the shipped ``fail_on=ADVISORY`` gate.

Pinned at the canonical tier because the non-fatality contract is what makes
the channel safe to add — a future refactor that promotes hints to fatal
under any severity is a compatibility break and must red here first.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.config_validation import (
    AuthoringReport,
    Finding,
)
from tolokaforge.core.models import GradingFindingSeverity

pytestmark = pytest.mark.canonical


@pytest.mark.parametrize(
    "fail_on",
    list(GradingFindingSeverity),
    ids=[member.value for member in GradingFindingSeverity],
)
def test_a_hint_only_report_is_never_fatal(fail_on: GradingFindingSeverity) -> None:
    """A report carrying only hints returns no fatal findings at any severity.

    ``ERROR`` returns errors alone; ``ADVISORY`` returns errors plus advisories;
    neither ever returns hints. A regression here would silently reintroduce the
    exact "every existing pack fails at load time" failure mode the plan added
    the ``hints`` channel to avoid.
    """
    report = AuthoringReport(
        hints=(Finding("llm_judge.rubric.criteria.clarity", "kind: graded, no anchor"),)
    )

    assert report.fatal(fail_on) == ()
    assert report.hints != ()
