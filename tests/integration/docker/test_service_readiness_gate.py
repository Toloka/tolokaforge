"""Integration locks for ``PerTrialRuntimeBackend``'s provision-time readiness
gate against a real Docker daemon.

A container that ``docker compose up --wait`` reports ``Healthy`` is not
necessarily reachable from the host: a service that binds only its container
loopback passes an in-container healthcheck yet drops the host's published-port
connection. These tests exercise the shipped gate — the default probe loader,
no seam injection — so the grpc/http/tcp probe kinds run against real
containers.

* Positive: a runner speaking h2c on the runner port plus an http-readiness and
  a tcp-readiness sidecar, all host-reachable, so provision succeeds and every
  probe kind is exercised. One ``nginx:alpine`` image with ``http2 on`` answers
  a gRPC channel, ``GET /health``, and a bare TCP connect — no runner image
  build, no API keys.
* Negative (#801-class): a runner that binds ``127.0.0.1`` only is Docker
  ``Healthy`` via its loopback healthcheck but host-unreachable, so provision
  fails at the gate with a :class:`DiagnosticPayload` naming the loopback
  listen address that the published port cannot reach.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.canonical._factories import make_task_description
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, NetworkPolicy, TrialSpec
from tolokaforge.runner.models import ReadinessSpec, ServiceSpec

pytestmark = [pytest.mark.integration, pytest.mark.docker]


_H2C_RUNNER = """\
server {{
    http2 on;
    listen {port};
    location /health {{ return 200 "ok\\n"; }}
    location / {{ return 200 "ok\\n"; }}
}}"""

_LOOPBACK_RUNNER = """\
server {{
    http2 on;
    listen 127.0.0.1:{port};
    location /health {{ return 200 "ok\\n"; }}
    location / {{ return 200 "ok\\n"; }}
}}"""


def _nginx_service(config: str, *, listen_probe: str, published_port: int) -> str:
    """Render an ``nginx:alpine`` compose service that installs ``config`` and
    health-gates on ``listen_probe`` (the address the in-container healthcheck
    hits), publishing ``published_port``."""
    escaped = config.replace("\n", "\\n").replace('"', '\\"')
    return textwrap.dedent(f"""\
        image: "nginx:alpine"
        command:
          - sh
          - -c
          - |
            printf "{escaped}" > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'
        ports:
          - "{published_port}"
        healthcheck:
          test: ["CMD", "wget", "-q", "-O", "-", "http://{listen_probe}/health"]
          interval: 2s
          timeout: 3s
          retries: 30
        """)


def _indent_service(name: str, body: str) -> str:
    inner = textwrap.indent(body.rstrip(), "    ")
    return f"  {name}:\n{inner}\n"


def _positive_compose() -> str:
    runner = _nginx_service(
        _H2C_RUNNER.format(port=50051), listen_probe="127.0.0.1:50051", published_port=50051
    )
    http_svc = _nginx_service(
        _H2C_RUNNER.format(port=8080), listen_probe="127.0.0.1:8080", published_port=8080
    )
    tcp_svc = _nginx_service(
        _H2C_RUNNER.format(port=9090), listen_probe="127.0.0.1:9090", published_port=9090
    )
    return (
        "services:\n"
        + _indent_service("runner", runner)
        + _indent_service("http_svc", http_svc)
        + _indent_service("tcp_svc", tcp_svc)
    )


def _loopback_compose() -> str:
    runner = _nginx_service(
        _LOOPBACK_RUNNER.format(port=50051), listen_probe="127.0.0.1:50051", published_port=50051
    )
    return "services:\n" + _indent_service("runner", runner)


def _write_manifest(compose_dir: Path, compose_text: str, **manifest_kwargs) -> EnvironmentManifest:
    compose_dir.mkdir(parents=True, exist_ok=True)
    compose_file = compose_dir / "docker-compose.yml"
    compose_file.write_text(compose_text + "\n")
    return EnvironmentManifest(
        compose_file=compose_file, runner_service="runner", **manifest_kwargs
    )


def _make_spec(manifest: EnvironmentManifest, trial_id: str) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id="readiness-gate-integration",
        task=make_task_description(
            task_id="readiness-probe",
            name="readiness-probe",
            category="general",
            description="provision-time readiness gate integration lock",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:8000",
            runner_url="http://placeholder:50051",
        ),
    )


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker daemon not available")
def test_readiness_gate_passes_for_grpc_http_and_tcp(tmp_path: Path) -> None:
    """A host-reachable stack passes the gate across all three probe kinds: the
    runner via the default grpc probe, plus http- and tcp-declared sidecars."""
    # full_internet: under no_internet, only the runner joins the egress-capable
    # edge network and gets a host-published port — non-runner services are
    # internal-only with no host mapping, so a host-side probe of an http/tcp
    # sidecar is only meaningful when those services are host-published.
    manifest = _write_manifest(
        tmp_path / "positive",
        _positive_compose(),
        network_policy=NetworkPolicy.FULL_INTERNET,
        services={
            "http_svc": ServiceSpec(isolation="ephemeral", readiness=ReadinessSpec(kind="http")),
            "tcp_svc": ServiceSpec(isolation="ephemeral", readiness=ReadinessSpec(kind="tcp")),
        },
    )
    backend = PerTrialRuntimeBackend(connect_timeout=20.0)
    handle = backend.provision(_make_spec(manifest, "readiness-positive:0"))
    try:
        # Reaching here means the gate accepted grpc + http + tcp; the handle is
        # only returned once every probe came back ready.
        assert handle.trial_id == "readiness-positive:0"
    finally:
        backend.teardown(handle)


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker daemon not available")
def test_readiness_gate_rejects_loopback_only_runner(tmp_path: Path) -> None:
    """A runner that binds container-loopback only is Docker ``Healthy`` yet
    host-unreachable — the gate fails provisioning with a diagnostic naming the
    loopback listen address the published port cannot reach."""
    manifest = _write_manifest(tmp_path / "negative", _loopback_compose())
    backend = PerTrialRuntimeBackend(connect_timeout=8.0)
    with pytest.raises(ProvisionError) as exc:
        backend.provision(_make_spec(manifest, "readiness-negative:0"))

    err = exc.value
    assert err.stage == "provision"
    assert err.diagnostic is not None
    assert err.diagnostic.service == "runner"
    assert err.diagnostic.kind == "grpc"
    assert err.diagnostic.result.ok is False
    # The published port maps to the container's external interface, but nginx
    # listens only on 127.0.0.1 inside the container — the diagnostic captures
    # that loopback listen address, naming the mechanism rather than the symptom.
    assert err.diagnostic.docker_port_map, "expected a real docker port map"
    assert any(
        addr.startswith("127.0.0.1:50051") for addr in err.diagnostic.container_listen_addrs
    ), f"expected loopback listen address in {err.diagnostic.container_listen_addrs!r}"
