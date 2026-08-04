"""Ordered health hierarchy for service-status responses.

Encapsulates the pattern: bounded state enum + ordered levels + semantic
predicates on a wrapper. Consumers ask domain questions
(``is_reachable``, ``is_fully_operational``) instead of string equality
against raw status values.

The runner's ``HealthCheck`` RPC (``docs/GRPC_PROTOCOL.md`` § HealthCheck)
is the reference use — its ``HealthCheckResponse.status`` values map to
:class:`HealthLevel` via :meth:`HealthReport.from_status`. The mapping
lives once, here, instead of scattered across every caller.

Extending the pattern: any service response with a ``status`` string that
carries an ordered health hierarchy should adopt this shape. Callers that
ask "is the service usable?" get a named predicate; callers that ask
"is everything fully up?" get a different named predicate; the domain
decision lives on the wrapper, not in each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class HealthLevel(IntEnum):
    """Ordered health hierarchy. Higher = more capable.

    Comparisons express intent: ``level >= DEGRADED`` reads as "at least
    reachable for RPCs"; ``level == HEALTHY`` reads as "fully operational,
    all downstreams up". Ordering is load-bearing — callers rely on
    ``>=`` / ``==`` for their semantic checks, so a future addition to
    the enum must be inserted at the correct rank.
    """

    UNHEALTHY = 0
    DEGRADED = 1
    HEALTHY = 2


# Mapping from runner's protocol status strings to HealthLevel.
# See docs/GRPC_PROTOCOL.md § HealthCheck for the authoritative status
# vocabulary. Unknown strings map to UNHEALTHY at the from_status site.
_STATUS_TO_LEVEL: dict[str, HealthLevel] = {
    "healthy": HealthLevel.HEALTHY,
    "degraded": HealthLevel.DEGRADED,
    "unhealthy": HealthLevel.UNHEALTHY,
}


@dataclass(frozen=True)
class HealthReport:
    """Semantic wrapper for a service HealthCheck response.

    ``level`` is the ordered classification; other fields carry context
    but do not participate in the domain decision. Callers should invoke
    the predicate methods (:meth:`is_reachable`, :meth:`is_fully_operational`)
    rather than reading ``level`` directly, so the mapping from state to
    domain question lives in one place.
    """

    level: HealthLevel
    version: str = ""
    detail: str | None = None

    def is_reachable(self) -> bool:
        """Can I invoke RPCs on this service now?

        True for ``HEALTHY`` and ``DEGRADED`` — both states mean the
        service's gRPC surface is up and answering. ``DEGRADED`` reports
        a downstream is unavailable, which is the service's own concern
        to surface via per-dependency warnings, not a signal callers
        should reject the service itself for connect-time reachability.
        """
        return self.level >= HealthLevel.DEGRADED

    def is_fully_operational(self) -> bool:
        """Are all downstream dependencies also up? True only for ``HEALTHY``.

        Use this when a caller genuinely needs the service AND its
        downstream stack — e.g. a grading run that will invoke the DB
        service through the runner. For connect-time reachability, prefer
        :meth:`is_reachable`.
        """
        return self.level == HealthLevel.HEALTHY

    @classmethod
    def from_status(
        cls,
        status: str,
        version: str = "",
        detail: str | None = None,
    ) -> HealthReport:
        """Parse a raw protocol status string into a :class:`HealthReport`.

        Unknown status values map to ``UNHEALTHY`` (fail-loud on protocol
        drift — an unknown state is treated as "don't use this service"
        rather than silently accepting it as reachable). Callers that
        need to distinguish "runner reported unhealthy" from "runner
        reported an unknown state" should inspect the original status
        string themselves; the HealthReport interface deliberately hides
        that distinction so predicates stay simple.
        """
        level = _STATUS_TO_LEVEL.get(status, HealthLevel.UNHEALTHY)
        return cls(level=level, version=version, detail=detail)
