"""Contract tests for the ordered health hierarchy pattern.

Pins :class:`~tolokaforge.core.health.HealthLevel`'s ordering,
:class:`~tolokaforge.core.health.HealthReport`'s semantic predicates, and
:meth:`HealthReport.from_status`'s parse behaviour. The pattern is
designed to be reused across service-status responses in the codebase;
this file locks the reference implementation.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.health import HealthLevel, HealthReport

pytestmark = pytest.mark.unit


class TestHealthLevelOrdering:
    """The enum is ordered: comparisons express intent."""

    def test_level_hierarchy_is_ascending(self) -> None:
        """``UNHEALTHY < DEGRADED < HEALTHY`` — order is load-bearing."""
        assert HealthLevel.UNHEALTHY < HealthLevel.DEGRADED
        assert HealthLevel.DEGRADED < HealthLevel.HEALTHY

    def test_at_least_reachable_reads_as_ge_degraded(self) -> None:
        """The intent 'at least reachable for RPCs' is written as
        ``level >= DEGRADED`` in call sites — verify that reads correctly."""
        assert HealthLevel.DEGRADED >= HealthLevel.DEGRADED
        assert HealthLevel.HEALTHY >= HealthLevel.DEGRADED
        assert not (HealthLevel.UNHEALTHY >= HealthLevel.DEGRADED)


class TestHealthReportPredicates:
    """The domain decision (is this service usable?) lives on the wrapper."""

    def test_is_reachable_true_for_healthy(self) -> None:
        assert HealthReport(level=HealthLevel.HEALTHY).is_reachable() is True

    def test_is_reachable_true_for_degraded(self) -> None:
        """Degraded means the service's own gRPC surface is up — a
        downstream dependency is the service's own concern to surface,
        not a reason for the client to reject reachability."""
        assert HealthReport(level=HealthLevel.DEGRADED).is_reachable() is True

    def test_is_reachable_false_for_unhealthy(self) -> None:
        assert HealthReport(level=HealthLevel.UNHEALTHY).is_reachable() is False

    def test_is_fully_operational_true_only_for_healthy(self) -> None:
        """Callers that genuinely need the whole stack (e.g. a grading
        run that will hit the DB service through the runner) ask this
        stricter question."""
        assert HealthReport(level=HealthLevel.HEALTHY).is_fully_operational() is True
        assert HealthReport(level=HealthLevel.DEGRADED).is_fully_operational() is False
        assert HealthReport(level=HealthLevel.UNHEALTHY).is_fully_operational() is False


class TestHealthReportFromStatus:
    """Protocol-string → HealthLevel mapping lives in one place."""

    @pytest.mark.parametrize(
        ("status", "expected_level"),
        [
            ("healthy", HealthLevel.HEALTHY),
            ("degraded", HealthLevel.DEGRADED),
            ("unhealthy", HealthLevel.UNHEALTHY),
        ],
    )
    def test_known_status_maps_to_expected_level(
        self, status: str, expected_level: HealthLevel
    ) -> None:
        """Three-state protocol vocabulary from ``docs/GRPC_PROTOCOL.md``
        § HealthCheck maps directly."""
        report = HealthReport.from_status(status)
        assert report.level == expected_level

    def test_unknown_status_maps_to_unhealthy(self) -> None:
        """A future protocol addition or a typo hits UNHEALTHY, not silent
        accept — fail-loud on drift. The client sees an unknown state and
        treats it as 'don't use this service' until the client is updated
        to understand the new state."""
        report = HealthReport.from_status("warming_up")
        assert report.level == HealthLevel.UNHEALTHY

    def test_carries_version_and_detail_metadata(self) -> None:
        """Context fields don't affect the domain decision but are
        preserved for logging / display."""
        report = HealthReport.from_status("degraded", version="1.2.3", detail="db unreachable")
        assert report.version == "1.2.3"
        assert report.detail == "db unreachable"
        # Predicates are unaffected by context.
        assert report.is_reachable() is True

    def test_empty_status_maps_to_unhealthy(self) -> None:
        """Empty string is neither known nor an intentional protocol
        value — treated as unknown/UNHEALTHY."""
        assert HealthReport.from_status("").level == HealthLevel.UNHEALTHY
