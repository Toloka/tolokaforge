"""Service-readiness Protocol seam, built-in probes, and in-memory fixture.

A readiness probe answers one question from the calling process's point of view:
given a resolved ``host:port`` and a timeout budget, is this service reachable
and protocol-ready *now*? It is deliberately cheaper than a full protocol
handshake — a gRPC channel becoming ready, an HTTP ``GET /health`` returning
2xx, or a TCP connect completing — and it knows nothing about the docker/compose
substrate that published the endpoint. That substrate-agnosticism is what lets
the same probe answer readiness whether the endpoint came from compose, a
remote host, or an in-memory fixture.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import grpc

HTTP_HEALTH_PATH = "/health"


@dataclass(frozen=True)
class ResolvedEndpoint:
    """The caller-process view of where a service is reachable."""

    host: str
    port: int


@dataclass(frozen=True)
class ReadinessResult:
    """Outcome of a single readiness probe: ``detail`` is ``None`` on success."""

    ok: bool
    latency_s: float
    detail: str | None = None


@dataclass(frozen=True)
class DiagnosticPayload:
    """Failure envelope for a readiness-gate probe that came back not-ready.

    Assembled at failure time from the live (not-yet-torn-down) container so the
    ``ProvisionError`` names the mechanism, not just the symptom: the resolved
    ``host:port`` the probe hit, the probe outcome, and the docker-side view of
    where the service actually listens. The docker-introspection fields
    (``docker_port_map`` / ``container_listen_addrs`` / ``per_network_ips``) are
    best-effort — empty structures when the docker calls fail, never a raise.
    """

    service: str
    kind: str
    endpoint: ResolvedEndpoint
    result: ReadinessResult
    docker_port_map: dict[str, str]
    container_listen_addrs: tuple[str, ...]
    per_network_ips: dict[str, str]


@runtime_checkable
class ServiceReadinessProbe(Protocol):
    def probe(self, endpoint: ResolvedEndpoint, *, timeout: float) -> ReadinessResult:
        """Answer whether ``endpoint`` is reachable and protocol-ready within ``timeout``."""
        ...


class GrpcReadinessProbe:
    """Ready when a gRPC channel to the endpoint reaches READY within the budget."""

    def probe(self, endpoint: ResolvedEndpoint, *, timeout: float) -> ReadinessResult:
        start = time.monotonic()
        address = f"{endpoint.host}:{endpoint.port}"
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=timeout)
            return ReadinessResult(ok=True, latency_s=time.monotonic() - start)
        except grpc.FutureTimeoutError as exc:
            return ReadinessResult(
                ok=False,
                latency_s=time.monotonic() - start,
                detail=f"gRPC channel to {address} not ready within {timeout}s: {exc}",
            )
        finally:
            channel.close()


class HttpReadinessProbe:
    """Ready when ``GET /health`` on the endpoint returns a 2xx within the budget."""

    def probe(self, endpoint: ResolvedEndpoint, *, timeout: float) -> ReadinessResult:
        """Redirects are followed by ``urlopen``'s default handlers, so the 2xx
        check applies to the final response — a ``/health`` that returns 3xx is
        ready when its redirect target is 2xx."""
        start = time.monotonic()
        url = f"http://{endpoint.host}:{endpoint.port}{HTTP_HEALTH_PATH}"
        try:
            with urllib.request.urlopen(  # noqa: S310 — scheme is the hard-coded http:// health URL, not user input
                url, timeout=timeout
            ) as response:
                status = response.status
            ok = 200 <= status < 300
            detail = None if ok else f"GET {url} returned HTTP {status}"
            return ReadinessResult(ok=ok, latency_s=time.monotonic() - start, detail=detail)
        except urllib.error.HTTPError as exc:
            return ReadinessResult(
                ok=False,
                latency_s=time.monotonic() - start,
                detail=f"GET {url} returned HTTP {exc.code}",
            )
        except (urllib.error.URLError, OSError) as exc:
            return ReadinessResult(
                ok=False,
                latency_s=time.monotonic() - start,
                detail=f"GET {url} failed: {exc}",
            )


class TcpReadinessProbe:
    """Ready when a TCP connect to the endpoint completes within the budget."""

    def probe(self, endpoint: ResolvedEndpoint, *, timeout: float) -> ReadinessResult:
        start = time.monotonic()
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=timeout):
                pass
            return ReadinessResult(ok=True, latency_s=time.monotonic() - start)
        except OSError as exc:
            return ReadinessResult(
                ok=False,
                latency_s=time.monotonic() - start,
                detail=f"TCP connect to {endpoint.host}:{endpoint.port} failed: {exc}",
            )


@dataclass(frozen=True)
class ReadinessProbeCall:
    endpoint: ResolvedEndpoint
    timeout: float


@dataclass
class ReadinessProbeCallLog:
    """Records every ``(endpoint, timeout)`` a fixture probe was called with."""

    calls: list[ReadinessProbeCall] = field(default_factory=list)

    def record(self, endpoint: ResolvedEndpoint, *, timeout: float) -> None:
        self.calls.append(ReadinessProbeCall(endpoint=endpoint, timeout=timeout))


class InMemoryServiceReadinessProbe:
    """Deterministic, network-free readiness probe for orchestrator-level tests.

    Precedence of the failure knobs: an explicit ``result`` is returned verbatim;
    else a non-``None`` ``fail_detail`` yields ``ok=False`` carrying it; else the
    ``ok`` shorthand decides success.
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        result: ReadinessResult | None = None,
        fail_detail: str | None = None,
    ) -> None:
        self.call_log = ReadinessProbeCallLog()
        self._result = result
        self._fail_detail = fail_detail
        self._ok = ok

    def probe(self, endpoint: ResolvedEndpoint, *, timeout: float) -> ReadinessResult:
        self.call_log.record(endpoint, timeout=timeout)
        if self._result is not None:
            return self._result
        if self._fail_detail is not None:
            return ReadinessResult(ok=False, latency_s=0.0, detail=self._fail_detail)
        return ReadinessResult(ok=self._ok, latency_s=0.0)


def grpc_readiness_probe_factory() -> GrpcReadinessProbe:
    return GrpcReadinessProbe()


def http_readiness_probe_factory() -> HttpReadinessProbe:
    return HttpReadinessProbe()


def tcp_readiness_probe_factory() -> TcpReadinessProbe:
    return TcpReadinessProbe()
