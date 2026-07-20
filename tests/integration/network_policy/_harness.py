"""Shared harness for the network_policy integration suite.

Materialises a minimal two-service stack through the real
:class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`, so
``manifest.network_policy`` is enforced by the shipped provisioning path
(``apply_network_policy_to_compose_file`` inside ``provision``) rather than
a test-only shim. The stack is:

* ``runner`` — ``nginx:alpine`` serving ``runner-ok`` on the runner gRPC
  port (:data:`RUNNER_PORT_DEFAULT`), published to the host and health-gated
  so ``docker compose up --wait`` blocks until it responds.
* ``app`` — ``curlimages/curl`` kept alive with ``sleep``; the service the
  test execs outbound ``curl`` from.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from testcontainers.compose import DockerCompose

from tests.canonical._factories import make_task_description
from tolokaforge.core.compose_materialisation import RUNNER_PORT_DEFAULT
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, NetworkPolicy, TrialSpec

RUNNER_SERVICE = "runner"
APP_SERVICE = "app"

PUBLIC_IP_URL = "http://1.1.1.1"
"""Raw-IP public target — probes egress without touching DNS."""

PUBLIC_DNS_URL = "http://example.com"
"""DNS-name public target — probes egress including name resolution."""

ALLOWLISTED_HOST = "example.com"
"""Stable IANA-reserved host used as the ``limited_internet`` allowlist entry."""

ALLOWLISTED_URL = f"http://{ALLOWLISTED_HOST}"
DENIED_URL = "http://example.org"
"""Stable host deliberately *absent* from the allowlist — proxy must deny it."""

INTER_SERVICE_URL = f"http://{RUNNER_SERVICE}:{RUNNER_PORT_DEFAULT}/"
RUNNER_OK_BODY = "runner-ok"


_COMPOSE_TEMPLATE = f"""
    services:
      {RUNNER_SERVICE}:
        image: "nginx:alpine"
        command:
          - sh
          - -c
          - |
            printf 'server {{ listen {RUNNER_PORT_DEFAULT}; location / {{ return 200 "{RUNNER_OK_BODY}\\n"; }} }}' > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'
        ports:
          - "{RUNNER_PORT_DEFAULT}"
        healthcheck:
          test: ["CMD", "wget", "-q", "-O", "-", "http://127.0.0.1:{RUNNER_PORT_DEFAULT}/"]
          interval: 2s
          timeout: 3s
          retries: 30
      {APP_SERVICE}:
        image: "curlimages/curl:8.10.1"
        entrypoint: ["sleep", "3600"]
    """

COMPOSE = textwrap.dedent(_COMPOSE_TEMPLATE).strip()


def write_manifest(
    compose_dir: Path, policy: NetworkPolicy, allowlist: list[str] | None = None
) -> EnvironmentManifest:
    """Write :data:`COMPOSE` into ``compose_dir`` and return a manifest that
    binds it under ``policy``. The manifest validators parse the compose file
    at construction, so the file is written first. ``allowlist`` threads the
    egress allowlist for ``limited_internet`` (empty for the other policies;
    the manifest's cross-field validator enforces the pairing)."""
    compose_dir.mkdir(parents=True, exist_ok=True)
    compose_file = compose_dir / "docker-compose.yml"
    compose_file.write_text(COMPOSE + "\n")
    return EnvironmentManifest(
        compose_file=compose_file,
        runner_service=RUNNER_SERVICE,
        network_policy=policy,
        limited_internet_allowlist=allowlist or [],
    )


def make_spec(manifest: EnvironmentManifest, trial_id: str) -> TrialSpec:
    """Build a :class:`TrialSpec` carrying ``manifest``. The endpoints and
    model config are placeholders — provisioning never connects the runner
    client (connect is lazy) and no LLM is invoked."""
    return TrialSpec(
        trial_id=trial_id,
        run_id="netpolicy-integration",
        task=make_task_description(
            task_id="netpolicy-probe",
            name="netpolicy-probe",
            category="general",
            description="network_policy egress-enforcement probe",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:8000",
            runner_url="http://placeholder:50051",
        ),
    )


def run_curl(
    compose: DockerCompose, url: str, *, max_time: int = 10
) -> subprocess.CompletedProcess:
    """Exec ``curl <url>`` inside the ``app`` service and return the completed
    process (never raises on non-zero exit). A blocked egress surfaces as a
    non-zero return code (curl 6 = DNS failure, 7 = connection refused,
    28 = timeout); a reachable target returns 0 with the body on stdout."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        APP_SERVICE,
        "curl",
        "--max-time",
        str(max_time),
        "-sS",
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=compose.context, check=False)


CURL_TRANSPORT_FAILURE = "000"
"""``%{http_code}`` sentinel curl emits when no HTTP response was received
(DNS failure, connection refused, timeout) — distinct from a proxy's 403."""


def run_curl_status(compose: DockerCompose, url: str, *, max_time: int = 15) -> str:
    """Exec ``curl`` inside the ``app`` service and return the HTTP status code
    as a string. Under ``limited_internet`` the app's egress is proxied, so a
    denied request still completes the proxy connection and returns ``403``
    (squid access-denied) rather than a transport failure — the status code is
    what distinguishes allowed (``200``) from denied (``403``). A genuine
    transport failure yields :data:`CURL_TRANSPORT_FAILURE`."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        APP_SERVICE,
        "curl",
        "--max-time",
        str(max_time),
        "-o",
        "/dev/null",
        "-sS",
        "-w",
        "%{http_code}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=compose.context, check=False)
    return result.stdout.strip()


def docker_container_ids() -> set[str]:
    """All container ids on the daemon (running or stopped)."""
    result = subprocess.run(["docker", "ps", "-aq"], capture_output=True, check=True, text=True)
    return set(result.stdout.split())
